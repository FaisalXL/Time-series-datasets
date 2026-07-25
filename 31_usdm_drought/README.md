# US Drought Monitor → CPT

> **Status: Rebuilt 2026-07-25.** **3,849 shippable records** (`public-domain-us-gov`)
> + **1,012 held** (`proprietary-review`, non-federal lead byline). 4,861/4,861 pass
> `validate.py --strict` with 0 warnings.

**What it is:** the weekly US Drought Monitor. One record = **one region section of one
weekly release** — that section's own prose from the official *National Drought Summary*,
paired with **only that region's** drought-area series over a **trailing 260-week window
ending on the week the section reports**.

**Record unit changed.** The retired build emitted one record per week: the *entire*
national narrative paired with an **expanding** window running back to 2000 (1,114–1,385
points, growing weekly). The text described one week; the series was 26 years of mostly
unrelated history, and every record re-shipped the same history. Now the text is one
region's paragraphs and the series is that region's own 260 weeks ending on that week.

#### 📄 Text — the section's own prose
| | |
|---|---|
| **What** | One `<region>` section of the National Drought Summary (Northeast, Southeast, South, Midwest, High Plains, West), or the national `<intro>` ("Summary") |
| **Source** | `droughtmonitor.unl.edu/services/data/summary/xml/usdm_summary_{YYYYMMDD}.xml` |
| **Format** | Structured XML → section text. Median 951 chars, min 200, max 6,369. |
| **`text_quality`** | `real`. **Nothing in `text` is written by this script** — `<ts></ts>` is appended directly to the section prose. Distinct texts: 3,849/3,849 (100%). |

> ⚠️ **robots.txt.** The retired builder fetched `/data/narrativepdf/…`, which is under
> `Disallow: /data/`. Python's `robotparser` reports `can_fetch=True` only because a UTF-8
> BOM breaks the first group; a BOM-stripping parser honours the rule. The
> `/services/data/summary/xml/` endpoint used now is outside every disallowed prefix, is a
> plain GET (no ASP.NET postback), and covers **all 1,386 weeks back to 2000-01-04**.
> `scripts/harvest.py` re-checks this on every run and asserts before fetching.

#### 📈 Time series — that region's drought coverage (6 channels)
| Channel (`unit`) | Meaning |
|---|---|
| `pct_area_d0_abnormally_dry` … `pct_area_d4_exceptional_drought` | % of the region's area at D0/D1/D2/D3/D4 **or worse** (cumulative, `statisticsType=1`) |
| `dsci_drought_severity_coverage_index` | Drought Severity and Coverage Index (0–500) |

Source: `usdmdataservices.unl.edu/api/RegionalClimateCenterStatistics/…` with `aoi=1..6` →
**High Plains, Midwest, Northeast, South, Southeast, West** — the six official USDM regions,
which are exactly the narrative's section headings. National records use
`USStatistics/…?aoi=us` filtered to `CONUS`. All 6 regions carry D0–D4 **and** DSCI at
1,386 weekly points, complete from 2000-01-04.

**Window:** `1w`, **trailing 260 weeks ending on the reported week** — 5 years, matching
`11_eia_petroleum_weekly` (the other weekly package). Length is a guarantee:
**100% exactly 260 points**, 6 channels each, **6,004,440 datapoints**. Weeks without a
full 260 weeks of history behind them (the first 259, 2000-01-04 → 2004-12-21) are skipped
rather than emitted short.

### Alignment — `describes`

**Structural, and exact in 100% of records:**

| Check | Result |
|---|--:|
| Window terminal point == the week the section reports | **3,849/3,849 (100%)** |
| `period_end` == the reported week | **3,849/3,849 (100%)** |
| Series region == the section's region | **3,849/3,849 (100%)** by construction |
| Cumulative ordering D0 ≥ D1 ≥ D2 ≥ D3 ≥ D4 holds | **3,849/3,849 (100%)** |

**Semantic — the prose's direction vs the data's:** classifying each paragraph by whether
degradation- or improvement-language dominates (margin ≥ 2) and comparing to the
week-over-week ΔDSCI:

| Subset | Direction agrees |
|---|--:|
| All decidable (n=1,172) | **83.5%** |
| \|ΔDSCI\| ≥ 10 (n=339) | **92.0%** |
| \|ΔDSCI\| ≥ 25 (n=68) | **95.6%** |

Agreement rises monotonically with the size of the move — when the series moves decisively
the prose almost always agrees, which is what a genuine pairing looks like. (2,583 records
have no dominant direction — USDM prose routinely reports improvement *and* degradation in
one region — and are excluded rather than guessed at.)

**Regional relevance:** 99.4% of regional records name their own region or one of its
states. The 18 that don't are legitimate short paragraphs ("this region remains free of any
dryness", "interior southern New England").

**Tier is `describes`, not `recites`:** only 0.36% of records state a terminal value
verbatim. The prose narrates *why* categories changed; it does not quote the percentages.

### Licensing — per-record, on the byline

The USDM site has **no rights page** and its footer asserts `©2026 – National Drought
Mitigation Center`; NDMC is at the University of Nebraska–Lincoln, **not a federal agency**,
so `public-domain-us-gov` is not defensible as a *package* label. The XML carries a
structured `<author><affiliation>` block, so the split is per-record and mechanical.

Only **10 distinct affiliation strings** appear across all 1,386 weeks, and every one
classifies unambiguously:

| Affiliation | Weeks | Class |
|---|--:|---|
| NOAA, NCEI · NOAA, NWS, NCEP, CPC · U.S. Department of Agriculture · NOAA, CPC · NOAA, NESDIS, NCEI · NCEI · NOAA, CPC, JAWF | 1,450 bylines | **federal** |
| National Drought Mitigation Center · WRCC, DRI · Western Regional Climate Center | 626 bylines | **non-federal** |

**Attribution rule: the first (lead) author owns the week.** 680 of the 1,386 weeks list
2–3 authors and the XML never says which author wrote which paragraph; the USDM credits the
lead author with that week's map and summary, so the lead byline is taken as authoritative
for the whole release. A week ships as `public-domain-us-gov` when its **first** author is
federal — **891 of 1,127 eligible weeks (79.1%)**, closely matching the 75% federal share
the corpus audit measured across all bylines. Weeks with a non-federal lead go to
`output/usdm_drought_quarantine.jsonl` as `proprietary-review`.

> This is a **licensing-owner decision** (Defu, 2026-07-25), not something the source
> states. A reviewer who reads each weekly narrative as a §201(a) joint work would instead
> quarantine every co-authored week; that stricter rule yields 668/1,127 weeks (59.3%) and
> 2,349 shippable records. Every record keeps its full `meta.authors` list and a
> `meta.byline_class`, so the call can be reversed by editing `week_license()` and
> rebuilding — no re-harvesting.

**If NDMC grants permission**, the held 1,012 records need no rework — only a `license`
field flip. Note the frozen v1 enum has no `permissive-attribution` slot; `cc-by-4.0` would
be a false identifier since NDMC never invoked Creative Commons.

### Reconcile — balances exactly

```
1,386 narratives available (1,386/1,386 fetched, 0 misses)
  − 259 weeks before a full 260-week window (2000-01-04 … 2004-12-21)
= 1,127 eligible weeks → 4,909 section-units considered
      3,849 emitted shippable
    + 1,012 emitted quarantine
    +    45 dropped: section prose < 200 chars
    +     2 dropped: same paragraph published under two <region> elements
    +     1 dropped: duplicate region section within a week
    = 4,909 ✓
```

The build **raises** if this does not balance. Also dropped upstream: 3,526 sections whose
label is not an official region name, and 292 eligible weeks with no `<intro>` element.

### Known limits (data, not bugs)

- **Coverage is era-dependent.** The narrative's section labels only settle onto the six
  official region names in 2018 (**6.0/week, every week, 2018–2026**). 2017 is transitional
  (5.25 mean); before 2017 the labels are merged and descriptive — *"The Plains, Midwest,
  and Great Lakes Region"*, *"The East"* — averaging 0.6–2.8 mappable sections/week. Those
  sections are **deliberately not mapped**: their footprint is not the polygon the series
  measures. Only a label that *is* an official region name (optionally + "Region") maps.
  Pre-2017 records were checked against post-2017 and are not weaker (99.5% vs 99.3%
  name-own-region; 88.0% vs 86.0% direction agreement), so the era is kept, not dropped.
- **Merged-label mapping is the remaining headroom** (~3,500 unmapped sections). It needs
  aggregated multi-region series and would loosen alignment; not taken.
- **Out-of-region spillover:** 16.9% of state mentions in pre-2017 regional prose name a
  state outside that region (6.9% post-2017) — normal editorial spillover in weeks with
  fewer, broader sections.
- **Non-CONUS sections** (Caribbean, Pacific, Alaska, Hawaii, Puerto Rico) have prose but no
  CONUS-region series, and are never emitted.

### Run

```bash
python scripts/harvest.py                    # robots check + 1,386 narratives + series
python scripts/build_cpt_jsonl.py            # full build (max_records: null)
python scripts/verify.py                     # final-inspection pass
python ../schema/validate.py output/ --strict
```

`.cache/` holds every narrative (`summary_xml/`) and API response (`api_rcc/`), so rebuilds
are offline and free. The stale `.cache/pdf/` (61 MB) is from the retired
robots-disallowed path and is no longer read by anything.

**Sources:** [US Drought Monitor](https://droughtmonitor.unl.edu/) (NDMC / NOAA / USDA) ·
[USDM web services](https://droughtmonitor.unl.edu/DmData/DataDownload/WebServiceInfo.aspx)
