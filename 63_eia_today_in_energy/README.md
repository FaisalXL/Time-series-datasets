# EIA "Today in Energy" + EIA Bulk Petroleum Series → CPT

> **Status: Demo built 2026-08-12 — 12 records, 6 channel bundles, 12 verified channels.**
> `schema/validate.py --strict` clean (12/12, 0 errors / 0 warnings).
> Scout-lane demo. The server agent runs the full build.
> **No licence gate and no API key** — this is the cheapest of the DR#6 candidates to ship.

**What it is:** one record = **one "Today in Energy" article** — its own **verbatim** body prose
paired with the trailing **36-step multi-channel window** of the EIA series the article is
actually about, with the pairing proved by **figure-level evidence**, per article.

```
"U.S. crude oil production grew by 3%, or 350,000 barrels per day (b/d), in 2025, setting a
 new annual production record of 13.6 million b/d, according to our latest Short-Term Energy
 Outlook (STEO)."
                          ↕  evidence-checked, per claim
 us_crude_oil_production_thousand_bd   2025 = 13586   ("13.6 million b/d", 0.10% off)
                                       2024 = 13235   (13586 − 13235 = 351 ≈ "350,000 b/d")
```

| | |
|---|---|
| **Domain / region** | energy / `US` |
| **License** | **U.S. government public domain** (`public-domain-us-gov`). No redistribution question. |
| **Alignment** | `recites` 12/12 — assigned by evidence, never assumed. `describes` is off by default. |
| **Freq / depth** | `1y`, `1M`, `1w` by bundle; 36-step window, `min_points` 32 |
| **Text** | median 1,707 chars (~427 tokens), max 2,000 (the 500-token cap), **100% verbatim** |
| **Access** | keyless, no login, no bot wall. Bulk zip + plain HTML. |

## Scale — enumerated, not estimated

`archive.php?my=YYYY` lists every article for a year as `detail.php?id=NNNNN`. Summing all 16
year pages and de-duplicating ids gives **exactly 3,567 articles** (2011 → 2026-08). The build
re-derives this number on every run, so it is self-checking.

| 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | 2017 | 2018 |
|---|---|---|---|---|---|---|---|
| 219 | 244 | 238 | 241 | 231 | 236 | 238 | 249 |

| 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 YTD |
|---|---|---|---|---|---|---|---|
| 246 | 240 | 246 | 245 | 199 | 221 | 172 | 102 |

Publication rate is **declining** (~170/yr recently), so 3,567 is close to a final total, not a
run rate. **Do not estimate the count from the article id** — ids increment ~18–20 per article
because the id space is shared with the rest of eia.gov (highest observed id 67926 ≠ 67,926
articles).

### But 3,567 is the article count, not the record count

The demo saw **231 articles and emitted 12** — a **5.2% yield**, because the six bundles
shipped here only cover **petroleum** topics and the evidence gate is strict. On the shipped
config that is **≈185 records**, not 3,567. The two ways up, in order of value:

1. **Add more bulk files.** `Today in Energy` covers electricity, natural gas, coal, renewables
   and nuclear; this config loads only `PET.zip`. EIA publishes `ELEC.zip`, `NG.zip`,
   `COAL.zip`, `TOTAL.zip` on the same keyless bulk route. Adding them is a **config-only**
   change (a `bulk_urls` entry plus bundles) — no build-script work. `no_bundle` was 191/231,
   so this is where the volume is.
2. Widen per-bundle channel sets so more of an article's figures have something to match.

**Do not raise the count by relaxing the evidence gate** — see below for why.

## The evidence gate is the whole quality story

The first demo build produced **12/12 records that passed `validate.py --strict`** and were
still wrong: a **hydropower** article wired to `crude_trade`, and a *"250-year history of U.S.
energy consumption"* article wired to `refining`. Schema validation cannot see a wrong pairing.
Three guards fixed it, all in `config.example.yaml` under `evidence:`:

| guard | what it stops |
|---|---|
| `match_window: 8` | Only the 8 most recent points are eligible targets. All 36 points × 4 channels ≈ 150 targets; at 1% tolerance a coincidental hit is nearly free. |
| `proximity_chars: 120` | The figure must sit near that **channel's own** keywords. Same cross-channel false-match fix ONS #61 needed after RBNZ #60 shipped one. |
| `require_title_keyword` | The article **title** must hit the bundle, not just the body. Body-only matching is what let the hydropower article in on a passing "exports". |

After the guards: 12/12 pairings topically correct, and **every evidence match re-audited
independently — 0 out of tolerance** (worst 0.98%, best 0.01%).

## Record shape (real — article 67404, 2026-03-31)

```json
{
  "text": "U.S. crude oil production grew by 3%, or 350,000 barrels per day (b/d), in 2025, ...\n\n<ts></ts>",
  "timeseries": [
    {"values": [..., 13235, 13586], "unit": "us_crude_oil_production_thousand_bd",       "freq": "1y"},
    {"values": [..., 20276, 21065], "unit": "us_total_petroleum_production_thousand_bd", "freq": "1y"}
  ],
  "task_type": "world_knowledge", "text_quality": "real",
  "alignment": "recites", "license": "public-domain-us-gov",
  "source": "https://www.eia.gov/todayinenergy/detail.php?id=67404",
  "meta": {"bundle": "crude_production",
           "evidence": [{"unit": "us_crude_oil_production_thousand_bd",
                         "series_value": 13586, "prose_figure": 13600000.0}]}
}
```

## Two traps this package already handles

**1. The mega-menu.** Scraping `<p>` from the whole page returns eia.gov's navigation
(*"Exploration and reserves, storage, imports and exports…"*) as if it were article prose. The
body must be cut to `<div class="tie-article">` **first**. This is the single easiest way to
silently poison the package, and the naive scrape looks fine until you read a record.

**2. No API key needed.** The report that surfaced this source flagged the EIA Open Data API v2
key as an access gate. It is not one for us: the **bulk zips are keyless** and carry the same
series. This package uses the bulk route only — the same trick EIA Petroleum Weekly #11 already
uses. `PET.zip` is 52 MB and caches to `.cache/`.

## Known caveat — STEO estimates vs PET reported data

Many `Today in Energy` articles quote **Short-Term Energy Outlook** figures, which are
estimates/forecasts, while the paired channels are **PET reported** data. They agreed on every
demo record (13.6 vs 13,586), but they are different products and can diverge on revision. The
same vintage question is documented in #11's config. `meta.bundle` records which series were
used so a mismatch is auditable after the fact.

## Freq tokens

`1y`, `1M`, `1w` — note `1M` is one **month**; `1m` is one **minute**. (#41 and #59 label
monthly series `1m` and pass validation silently.)

## Run it

```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run
python scripts/build_cpt_jsonl.py                                 # demo, 12 records
python scripts/build_cpt_jsonl.py --set output.max_records=null   # full build
python ../schema/validate.py output/eia_today_in_energy_cpt.jsonl --strict
```

**Attribution required by the licence:** *"Source: U.S. Energy Information Administration,
Today in Energy"* — carried on every record in `meta.attribution`.
