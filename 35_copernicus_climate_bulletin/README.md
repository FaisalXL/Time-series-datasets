# Copernicus C3S Climate Bulletin → CPT

> **Status: ✅ Finalized — 825 records** (was a 119-record bank, 6.9×). This was the last
> banked-but-uninspected package, so the **Banked** bucket is now empty.

**What it is:** one record = **(theme × reported month × the bulletin's own narrative section)**
— that section's prose, verbatim, paired with the series behind *that section's own figure*.
C3S publishes the CSV for every figure it draws, so the numbers are the ones the prose was
written against; there is no third-party series to reconcile and no as-published-vs-revised
question of the `47/48/49/50` kind.

| | |
|---|---:|
| Records | **825** |
| Timesteps / datapoints | 83,167 / 358,705 |
| Records ≥ 32 points | **100%** |
| Nulls | **0.00%** |
| Channels per record | median 2, max 16 |
| Reported-month span | **2017-04 → 2026-06** (100 distinct months) |
| Text | median **1,211 chars**, 825/825 distinct |
| `alignment` | 152 `recites` / 673 `describes` (measured per record) |
| Themes | hydrological 365 · temperature 266 · sea ice 194 |

---

## The three findings that define this build

### 1. The bulletin has three themes; only two were built

The **bulletin navigation on every page** names them: Surface air temperature, Sea ice cover,
**Hydrological variables**. The third was never built. It is 111 months with its own figure
data — global and European relative humidity monthly since 1979, plus four-month means of
precipitation, soil moisture, temperature and humidity for **four European sub-regions**
(NW/NE/SW/SE) — and it is now the largest theme in the package at 365 records.

### 2. The universe is 352 theme-months, not 180

Enumerated from the Wayback CDX (1,588 dated bulletin URLs) and confirmed live. Six URL slugs
across the eras, each **tried** per month rather than assumed, so the boundaries are measured
output rather than constants that can be silently wrong:

| theme | slugs | months | span |
|---|---|--:|---|
| temperature | `surface-air-temperature`, `average-surface-air-temperatures` | 129 | **2015-08** → 2026-06 |
| sea ice | `sea-ice-cover`, `sea-ice` | 112 | 2017-03 → 2026-06 |
| hydrological | `precipitation-relative-humidity-and-soil-moisture`, `…-soil-moisture-and-river-flow` | 111 | 2017-04 → 2026-06 |

**352 pages harvested, 0 unresolved fetches**; 65 months absent at source (all before a
theme's first bulletin). The banked build's `start_month: 2019-01` lost 41 months.

### 3. Each page is natively sectioned — a topic × period grid

`<h2>` names the topic, `<h3>` the period, giving **1,042 sections across 352 theme-months
(~3.0/month)** where the banked build emitted one record per page. That is the corpus audit's
"≈1.9 topics/bulletin" explained and fixed.

Two `<h3>`s on one page routinely carry the *same* text — "July 2025" appears under Global
average, European average *and* Regional overview — so a section is only unique together with
its parent, and the key carries both.

### And the expanding window is gone

The banked temperature records carried **505–1038 monthly points**, re-shipping ERA5's whole
history every month. The bulletin's headline claim is a *rank within a calendar month* ("the
sixth warmest January on record") and C3S publishes exactly that series — one point per year
for that month, **1940→present**. A section about the reported month now gets the series its
claim is about: bounded, aligned, and 86 points deep. The sea-ice half already worked this
way; this generalises it.

| series family | records | median points | stride |
|---|--:|--:|---|
| `rh_monthly` | 241 | 120 | monthly |
| `sie_calendar_month` | 194 | 44.5 | annual (one July per year, 1979→) |
| `sat_allmonths` | 130 | 120 | monthly |
| `hydro_4month` | 124 | 120 | monthly |
| `sat_12month` | 67 | 120 | monthly |
| `sat_calendar_month` | 46 | 86 | annual (1940→) |
| `sst_daily` | 23 | 365 | daily |

---

## Alignment — measured (`--audit-alignment`)

**Structural: 825/825 on all four** — every channel exactly `meta.n_points` long · terminal
point non-null · exactly one `<ts></ts>` · the series ends on the month the bulletin reports.

That last one is a hard requirement, not an observation: bulletins sometimes link an earlier
month's figure CSV alongside their own, and without the check this month's prose would be
paired with another month's terminal point (696 channel-candidates were rejected by it).

**A stated rank reproduces from the series it is a claim about.** On the 92 sections that make
exactly one rank claim: **52.2% exact, 89.1% within one place**. 87 further sections make
several rank claims about different quantities and are *not scored* — an automated reader
cannot attribute them to one channel, and hand-checking showed that scoring them measures the
reader rather than the pairing (the 2020-10 Arctic record is exactly right — terminal −2.978,
matching the prose's "3.0 million km² below", ranked 1st of 42 as the prose says — yet the
multi-claim reader scored it a miss).

**Stated above/below average agrees with the terminal sign in 471/640 = 73.6%**, against a 50%
chance baseline.

**Tier, with a permutation control:** the prose quotes a channel's terminal value in
**18.4%**, against **5.8%** for the same channel set from *another* month (+12.6 pp).

⚠️ **Value quotation is weak here, and the reason is the source, not the pairing: 52% of the
figures the prose states are not values in the record's series.** Characterised rather than
hidden — the residual is dominated by quantities an aggregate anomaly series cannot hold:

- **local extremes** — "exceeding 10 °C above average in the Ross Sea sector" (the record's
  channel is the global or European mean)
- **other periods** — a monthly section quoting the 12-month or calendar-year figure
- **impact reporting** — "200 mm of rain", "1300 lives", "33 million people"
- **absolute magnitudes** — "17.9 million km², which is 0.2 million km² below average": the
  *anomaly* is in the series, the absolute is a different quantity

Coordinates, the 1.5 °C policy threshold, durations and impact units are excluded from the
count outright (they cannot be anomaly readings); the classes above are genuinely quoted
values that the section's own aggregate series correctly does not contain. **This package's
alignment evidence is the structural guarantees, the rank check and the sign check — not a
value-quotation rate.**

---

## Reconcile (the build raises if it does not balance)

352 bulletins → 1,042 sections.
**1,042 = 825 emitted + 135 no-series-for-section + 57 all-channels-unusable + 20 short-text
+ 5 duplicate-text ✓**

---

## Defects fixed at inspection

| # | Defect | Effect |
|---|---|---|
| 1 | Hydrological theme never built | +365 records |
| 2 | `start_month: 2019-01`; record unit = whole page | 180 → 352 theme-months; 1 → ~3 records per bulletin |
| 3 | Expanding 505–1038-point window | replaced by the calendar-month series the ranking prose is about |
| 4 | **`"arctic" in "antarctic"` is `True`** | all 112 Antarctic figure files were classified as Arctic; the Antarctic channel would have vanished into the Arctic's |
| 5 | **A period-range key parsed *wrongly* rather than failing** — one era keys a 4-month mean as `197901 to 197904` | the reader took `to` and the end-month as data values. Now keyed on the range end, matching the convention the modern files state |
| 6 | Figure numbers churn (`Fig1b`/`Fig3b`/`Fig6b` are all `global_allmonths`) | matching moved to the descriptive part of the filename; a figure-number matcher loses whole eras |
| 7 | Monthly data stamped `1940-01-01` was labelled daily | 40 files were publishing freq `1d` for a monthly series; frequency is now inferred from the key set |
| 8 | Column names change case and code across eras (`Global`/`global`, `tp`→`MTPR`) | published 1 channel where the file held 2, and named four distinct variables identically |
| 9 | Whitespace-delimited files with opaque names (`Data_for_month_10_2017_plot_5.csv`) | returned nothing until classification moved to the file's own title line |
| 10 | `_unit_name` ignored the column | multi-column files produced duplicate unit names, which `--strict` rejects: **358 records were being lost** |
| 11 | **A combined Arctic+Antarctic file was attached wholesale** | the Antarctic record carried an `antarctic_arctic` channel — the cross-contamination the per-section design exists to prevent. Region-named columns are now restricted to the record's own region (0 remain) |
| 12 | **Baseline was *assumed* to be 1991-2020 when the filename lacked one** | the older sea-ice files are 1981-2010. Since Antarctic sea ice declined, the same month is a *negative* anomaly against 1981-2010 and *positive* against 1991-2020 — the wrong label inverted the sign relative to the prose. Now read from the file's own header |
| 13 | **"More rows wins" picked the wrong *quantity*** | a bulletin links both an extent file (`CIE`) and an area file (`CIA`); row count chose area, pairing "0.8 million km² below … third lowest July extent" with an area anomaly of −0.38. The named quantity now ranks ahead of coverage |
| 14 | Series baseline vs the baseline the prose quotes | the section's own text names its reference period; that now drives file selection |
| 15 | Captions | identified structurally (C3S italicises them — holds in 379/380 paragraphs), never by keyword. 0 caption leakage and 0 script blobs in the output |

---

## Exhaustion and the real ceiling

**The wall is figure data, not narrative.** C3S began publishing downloadable figure CSVs at
**2017-03/04** (sea ice, hydrological) and **2017-11** (temperature):

| theme | pages | with figure data | without |
|---|--:|--:|---|
| temperature | 129 | 101 | 26 pre-2018 + 2024-08/09 |
| sea ice | 112 | 108 | 2024-08, 2024-09, 2025-01, 2025-03 |
| hydrological | 111 | 107 | 2024-08, 2024-09, 2025-01, 2026-06 |

- ⚠️ **The 26 pre-2018 temperature bulletins have narrative but no figure data at all**, so
  they cannot be paired. That is the ceiling, and it is a source fact — the pages are cached,
  so if C3S ever backfills the data the build picks them up with no code change.
- ⚠️ **A handful of recent months (2024-08/09, 2025-01, 2025-03, 2026-06) also link no CSV** —
  those pages embed their figures through a JS component instead. Worth re-checking
  periodically; `harvest.py --report` shows it.
- **1,473 of 1,506 linked figure CSVs are cached.** Of the 33 outstanding, 3 are `http_404`
  and 2 are the site's HTML shell (missing at source); the rest are connection errors, which
  a re-run of `--csvs` retries — a network failure is never recorded as an absent file.

---

## Run

```bash
pip install -r requirements.txt
python scripts/harvest.py --pages            # 352 bulletin pages, all slugs tried
python scripts/harvest.py --csvs --workers 4  # every figure CSV the pages link
python scripts/harvest.py --report            # coverage, slug eras, unresolved fetches
python scripts/build_cpt_jsonl.py
python scripts/build_cpt_jsonl.py --audit-alignment   # everything in the section above
python scripts/build_cpt_jsonl.py --audit-vintage     # ERA5 revision between bulletins
```

**Scripts:** `c3ssrc.py` universe/slug map · `harvest.py` polite cached fetch (thread pool over
one shared rate limiter) · `c3sdata.py` figure-CSV parsing + family classification ·
`c3ssec.py` HTML sectioning, caption rule, figure/rank/sign extraction · `polite_fetch.py`
(ported from `56`).

**License:** Copernicus licence — free reuse with attribution to C3S/ECMWF. Tagged
`cc-by-4.0` as the closest schema-enum fit, with the real licence in `meta.true_license`
(the `52_statcan_daily` convention).

**Sources:** [C3S Climate Bulletin](https://climate.copernicus.eu/climate-bulletins) ·
[about the data and analysis](https://climate.copernicus.eu/climate-bulletin-about-data-and-analysis).
