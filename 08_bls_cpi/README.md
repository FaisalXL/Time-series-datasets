# BLS CPI → CPT

> **Status: Demo done** — 50 records. Full build ~389 records (1994–2026). Full run pending server.

**What it is:** BLS Consumer Price Index monthly press release paired with a 12-month rolling window of CPI index values from the BLS public API. One record = **one monthly release**.

**Scale:** ~389 monthly releases (Feb 1994 – Jun 2026), zero expected drops from incomplete TS (BLS API data goes back to the 1940s–1970s for all five series).

| Source | Period | Releases | Format |
|--------|--------|----------|--------|
| `bls.gov/news.release/history/` | Feb 1994 – Dec 2007 | ~167 | Plain text (`.txt`) |
| `bls.gov/news.release/archives/` | Jan 2008 – Jun 2026 | ~222 | PDF (`.pdf`) |
| **Total** | **1994–2026** | **~389** | |

**Record shape:**
```json
{
  "text": "Consumer prices for all urban consumers rose 0.3 percent in May 2026... Monthly CPI indicators for the 12 months ending 2026-05: <ts></ts>",
  "timeseries": [
    {"values": [308.4, 309.1, ..., 314.1], "unit": "cpi_u_all_items_sa",  "freq": "1M"},
    {"values": [307.0, 307.8, ..., 312.9], "unit": "cpi_u_all_items_nsa", "freq": "1M"},
    {"values": [320.5, 321.2, ..., 325.4], "unit": "cpi_u_core_sa",       "freq": "1M"},
    {"values": [285.3, 286.0, ..., 290.2], "unit": "cpi_u_food_sa",       "freq": "1M"},
    {"values": [215.4, 216.9, ..., 222.5], "unit": "cpi_u_energy_sa",     "freq": "1M"}
  ],
  "task_type": "world_knowledge",
  "text_quality": "real",
  "data_month": "2026-05",
  "release_date": "2026-06-10"
}
```

**Text sources:**
- **2009–2026** — HTML press releases via Internet Archive Wayback Machine (bls.gov blocks direct access). ~123 releases discoverable; ~70 accessible from Wayback.
- **1994–2008** — Plain-text (`.txt`) press releases fetched directly from `bls.gov/news.release/history/`. Enable with `data.include_txt: true` in config (requires direct bls.gov access).

**Key issues:**
- **12-month rolling window** — each record's TS spans the 12 months ending at `data_month`. Any record with a missing month in any series is dropped.
- **Series ID verification** — `CUSR0000SAF` (food) and `CUSR0000SA0E` (energy) should be checked at [data.bls.gov/timeseries](https://data.bls.gov/timeseries/) if `skipped_incomplete_ts` is unexpectedly high. All series IDs are configurable in `config.example.yaml`.
- **API rate limit** — BLS API v1 = 25 queries/day; responses are cached after first fetch so reruns don't count against the limit.
- **Wayback gaps** — ~52 of 123 CDX-discovered HTML URLs return 503 from Wayback; these are skipped (counted as `skipped_no_text`).

**Run:**
```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py                            # demo (50 records, HTML only)
python scripts/build_cpt_jsonl.py --set output.max_records=10  # smoke test
python scripts/build_cpt_jsonl.py --set output.max_records=null  # full HTML build (~70 records)
# Full build with TXT (1994–2026, ~250+ records):
python scripts/build_cpt_jsonl.py --set output.max_records=null --set data.include_txt=true
```

**Output:** `output/bls_cpi_cpt.jsonl` · **Sources:** [BLS CPI Archive](https://www.bls.gov/bls/news-release/cpi.htm) + [BLS Public API](https://www.bls.gov/developers/)
