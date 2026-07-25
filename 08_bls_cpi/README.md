# BLS CPI → CPT

> **Status: ✅ Finalized 2026-07-25 — 2,752 records.** 389 data months (1994-01 → 2026-06),
> 330,240 timesteps, 2,586,000 datapoints. 2,752/2,752 pass `schema/validate.py --strict`
> with 0 warnings.

**What it is:** one record = **(data month × narrative section of that month's CPI news
release)**. A CPI release is natively sectioned, and each section is a self-contained block
of prose about one group of CPI indexes, so the release itself supplies the split. That
section's verbatim prose is paired with a **trailing 120-month window of the index levels
that section is about**, ending on the month the section reports.

**Record = one section, not one release.** The split is the source's own and it changed
once, in 2010:

| Release format | Data months | Sections the release itself delimits | Records |
|---|---|---|--:|
| **Group era** | 1994-01 … 2009-11 | lede (CPI-U level) · `CPI for All Urban Consumers (CPI-U)` summary · one paragraph each for food and beverages, housing, transportation, apparel, medical care, recreation, education and communication, other goods and services · `CPI for Urban Wage Earners…(CPI-W)` · `Chained…(C-CPI-U)` | 1,731 |
| **Heading era** | 2009-12 … 2026-06 | summary · `Food` · `Energy` · `All items less food and energy` · `Not seasonally adjusted CPI measures` | 1,021 |

Plus `Year in Review`, a December-only calendar-year section BLS published through 2020
(18 records) and then discontinued.

```
all_items 387 · food 381 · energy 202 · core 202 · nsa 202 · cpi_w 187 · housing 186
transportation 183 · medical 178 · apparel 176 · other 173 · educ_comm 120
recreation 112 · c_cpi_u 45 · annual_review 18
```

## Alignment

`alignment` is set per record by a reproducible test, not asserted: **`recites`** (554) when
a channel's terminal — reported-month — index level appears verbatim in that section's
prose, **`describes`** (2,198) otherwise.

| Check | Result |
|---|---|
| Window ends on the month the section reports | **2,752 / 2,752 (100%)** |
| First channel is the topic's anchor index, non-null at that month | **2,752 / 2,752 (100%)** |
| Series length | **100% exactly 120 points**, all channels equal length |
| Verbatim recite of a terminal index level | 554 / 2,752 (20.1%) |
| … same test against **another month's prose for the same topic** (permutation control) | **0 / 2,752 (0.0%)** → lift **+20.1 pp** |
| Stated over-the-year change on the anchor reproduces from the series | **422 / 423 (99.8%)**; **423 / 423 (100%)** against some channel of the record |
| Distinct section prose | **2,752 / 2,752 (100%)** |
| Null datapoints | 16,941 / 2,586,000 (0.66%) |

The recite tier concentrates exactly where the release recites levels rather than percent
changes: `nsa` 98.0%, `cpi_w` 96.3%, group-era `all_items` ~94% (45.0% across both eras,
since the heading-era summary quotes only percent changes). Everything else states percent
changes, which are derived from two series values rather than being one — hence
`describes`, but a strong one: 99.8% of the stated over-the-year figures reproduce to the
published decimal from the topic's anchor channel. The single exception is not an error —
2016-06 `core` states 2.3% where the NSA anchor gives 2.2447% (→ 2.2), because that release
quoted the change on the **seasonally adjusted** core index, which gives 2.2622% (→ 2.3).
That channel is the record's second one, so **423/423 (100%) reproduce against some channel
the record actually carries.**

> ⚠️ **Core caveat — seasonally adjusted channels are as-revised, not as-published.** BLS
> re-estimates seasonal factors every February and revises the previous **five years** of
> seasonally adjusted data, so a 2005 release's SA figures no longer match today's SA
> series. Measured: only **38.2%** of stated 1-month SA changes reproduce, and the rate
> tracks the revision schedule exactly — **2026: 100% · 2025: 59% · 2024: 56% · pre-2020:
> 23–47%**. This is the same as-published-vs-revised effect `07_cdc_fluview` and
> `24_noaa_swpc` document. It is why **every topic's anchor channel is a
> not-seasonally-adjusted index**: NSA indexes are final when published and never revised,
> so each record carries at least one channel a stated number can be checked against
> exactly.

## Exhaustion

**Complete for the web era, zero real gaps.**

- Wayback CDX over `bls.gov/news.release/{history,archives}/cpi_*` yields **389 release
  dates** (txt 168 · htm 221 · pdf 289 by availability). `scripts/fetch_all.py` retrieved a
  document for **389/389**.
- Those 389 releases map to **389 distinct data months, 1994-01 → 2026-06**, i.e. **12/12
  months in every year** except 2025 (11) and the partial 2026 (6).
- The span is 390 months, so exactly **one month is absent: 2025-10** — BLS never published
  an October 2025 CPI (lapse in appropriations). The November release (2025-12-18) is
  genuinely abbreviated for the same reason, and reports 2-month changes.
- **Data month is parsed from the release's own title line**, not from `release month − 1`.
  That rule is wrong for 1996-02-01, which carries the December 1995 CPI delayed by the
  1995–96 shutdown; deriving it would have collided with the real 1996-01 release and lost a
  month.
- Pre-1994 releases are not on the web at all — 1994-02 is the earliest capture in either
  directory.

**Reconcile balances, and the build raises if it does not:**

```
389 releases → 2,873 sections found
            = 2,752 emitted
            +   102 anchor index does not cover the window
            +    13 section prose truncated in the source document
            +     6 section shorter than min_text_chars (150)
```

- **102 no-anchor:** the anchor index starts mid-window — C-CPI-U 1999-12 (40 of 85 c_cpi_u
  sections), recreation and education-and-communication 1993-01 (54 + 10). A 120-point
  channel that is 90% null is not a series, so the record is dropped rather than padded.
- **13 truncated:** the **BLS document itself** stops mid-sentence (e.g. 2002-06-18's food
  paragraph ends *"…alcoholic beverages--each increased 0.2 percent,"*). Verified identical
  in both the `.txt` and the `.pdf` rendering, so it is the release, not the capture. Same
  call as `41_wasde`'s 15 garbled-PDF drops.

## Provenance

`bls.gov` returns **HTTP 403 to automated access** (verified, both `www.` and
`download.bls.gov`), so every release document comes from `web.archive.org`.
`meta.report_url` is the canonical bls.gov URL and `meta.fetch_url` the archive snapshot
actually read. Series come from **`api.bls.gov/publicAPI/v1`**, which is keyless but caps a
request at 25 series / 10 years and an IP at **25 requests/day** — 64 series × 5 decade
chunks = 15 requests for a cold cache, all cached under `.cache/api`, so reruns are free.

**64 CPI series, 1984-01 → 2026-06**, all probed against the API. `meta.channels` and
`meta.bls_series_ids` name exactly what each record carries, and the `<ts>` sentence names
the same channels in the same order.

## Files

| Path | Role |
|---|---|
| `scripts/fetch_all.py` | Enumerate + harvest all 389 release documents from Wayback |
| `scripts/cpi_series.py` | The 64 series each section is about; cached BLS API loader |
| `scripts/cpi_text.py` | Release → its own narrative sections (both eras) |
| `scripts/build_cpt_jsonl.py` | Section × trailing window → records; reconcile gate |
| `scripts/census.py` | Read-only: section structure across all 389 releases |
| `scripts/inspect_build.py` | The final-inspection measurements quoted above |

## Run

```bash
pip install -r requirements.txt
python scripts/fetch_all.py           # 389 documents (idempotent, ~12 min cold)
python scripts/cpi_series.py          # 15 API requests cold, 0 warm
python scripts/build_cpt_jsonl.py     # full build
python ../schema/validate.py --strict output/bls_cpi_cpt.jsonl
python scripts/inspect_build.py
```

**Output:** `output/bls_cpi_cpt.jsonl` · **License:** `public-domain-us-gov` (US federal
work, all 2,752 records) · **Sources:**
[BLS CPI news releases](https://www.bls.gov/bls/news-release/cpi.htm) ·
[BLS Public API](https://www.bls.gov/developers/)

## Headroom (deliberately not taken)

- **Sub-index sections.** The `core` and `food` sections name indexes finer than the 64
  series carried (food at employee sites and schools, toys, cable and satellite television).
  Adding them lengthens channel lists without adding records.
- **Regional and local-area CPIs.** Tables 3/6 of every release carry four census regions,
  three city-size classes and 23 local areas — but they are *tables*, with no narrative of
  their own. Pairing them with the national prose would be boilerplate reuse.
- **C-CPI-U / recreation / educ_comm before their series start.** Would need a shorter
  window for those topics only, breaking the uniform 120-point guarantee for ~102 records.
