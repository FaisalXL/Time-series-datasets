# Philadelphia Fed Manufacturing Business Outlook Survey (MBOS) → CPT

> **Status: ✅ Finalized 2026-07-26 — 1,620 records** (213 before this pass). One of the Federal Reserve regional business surveys — **sibling datasets** (NY Empire State, Richmond, Dallas TMOS, Kansas City, …) are separate packages; see [fed_surveys_discovery.md](../../docs/fed_surveys_discovery.md).

**What it is:** One record = **(release month × indicator)** — the sentence(s) of one MBOS release that describe a single diffusion index, paired with a **trailing 36-month window of that index alone**, at the vintage the release itself published. 264 release months, 11 indicators, 2001-04 → 2026-07.

**No generated text.** A record's `text` is the Bank's own sentences followed by `<ts></ts>` — no provenance header, no channel-naming sentence, nothing written by the build script. Provenance lives in `meta` (bank, survey, district, `release_month`, `topic`, `anchor_cell`, `channels`). Earlier drafts of this build carried a header plus a channel-naming sentence plus a per-topic world-knowledge paragraph, which left the Bank's own words at 26–34% of the record; they are gone.

## The two things that make this package what it is

### 1. The series is the **as-first-published (real-time) vintage**, not `bos_dif.csv`

The Bank re-estimates the survey's seasonal factors and **revises the whole history**. Measured over the 1,620 records: the value a release states matches today's published CSV in **0–3% of records in every year except 2026** (100%), with a median absolute drift of **≈2.2 index points**. The June 2015 release states current activity at **15.2**; `bos_dif.csv` now says **8.2**.

So the CSV cannot be the series — the text would be describing numbers that are no longer there. Instead every release prints its own table of what it published that month, and stitching those tables gives the vintage the prose actually quotes:

```
<label>          prev_idx  inc  no_chg  dec  idx  |  prev_idx  inc  no_chg  dec  idx
                 \____ this month vs last ______/    \____ six months from now ____/
```

Two consequences worth knowing:
- The text's numbers are **checkable**, so `alignment` is measured per record rather than asserted.
- A real-time MBOS vintage **is not published anywhere else** — which retires the old "these indices are already on FRED, get overlap sign-off" caveat. The FRED series is the revised one.

### 2. Per-indicator split, tight alignment

A release narrates general activity, new orders, shipments, employment, the workweek, both price indexes and the six-month outlook in **its own separate sentences**. Each becomes its own record with only that indicator's series attached. A sentence naming **three or more** indicators is a release summary, not one topic's prose, and is dropped rather than filed under whichever it names first.

## 📄 Text — the release's own sentences

| | |
|---|---|
| **What** | The clauses of that month's narrative that are about this indicator, and nothing else. Median 283 chars (min 131); **95.5% distinct** — see the note below. |
| **Source** | 2008-01→now: `…/mbos/{YYYY}/bos{MMYY}.pdf`. 2002-01→2007-04: the Bank's per-era archive ZIPs (`…/mbos/archives/{YYYY}_{YYYY}.zip`). 1997-09→2001-12: the retired `phil.frb.org` plain-text layout via Wayback. |
| **Clause trimming** | The Bank routinely narrates two indicators in one sentence. Kept whole, that sentence puts a *shipments* figure inside a *new orders* record — 15.8% of all figures were orphans like that before this pass. Single-topic sentences are left verbatim; genuinely joint ones are cut at the conjunction, and any clause still quoting a value the record's own series lacks is dropped (157 clauses). |
| **Extraction** | `scripts/colex.py` — column-aware. Poppler's own reading order **zips the two (2008-2015: three) columns line by line and shreds sentences**: *"The de(10 percent), although 76 percent…mand for manufactured goods"*. The old builder recovered a median of **682 chars of 7,530**; this one recovers **3,481**. |
| **`text_quality`** | `"real"`, literally: the record contains no script-written text at all. |

## 📈 Time series — as-published diffusion indexes

6 channels (3 for capital expenditures, which the survey only asks about the six-month outlook), **exactly 36 monthly points** in every record — sized to what the prose reaches back over, measured at a median of 21 months and p75 36 across 272 "since &lt;month&gt;" references:

| `unit` | table cell |
|---|---|
| `diffusion_index_current` | this month's index (**anchor**) |
| `percent_of_firms_reporting_increase_current` | % increase |
| `percent_of_firms_reporting_decrease_current` | % decrease |
| `diffusion_index_six_month_outlook` | six-month-ahead index |
| `percent_of_firms_expecting_increase_six_month` | % expecting increase |
| `percent_of_firms_expecting_decrease_six_month` | % expecting decrease |

Indicators: general activity · new orders · shipments · unfilled orders · delivery times · inventories · prices paid · prices received · employment · average workweek · capital expenditures.

`scripts/tabex.py` reads the table from PDF word geometry (rows by nearest label, columns by x-cluster) and `scripts/txtex.py` reads the fixed-width ASCII version. **Two column layouts exist and neither announces itself** — 2004+ leads with a Previous Index column, 1997-2003 orders the cells Decrease/No change/Increase/Index — so both are tried and the one satisfying `index == %increase − %decrease` on more rows wins. That identity is also the parse guard: it holds on **100%** of emitted rows, and a row shifted by one fails it immediately.

## Verified numbers

| | |
|---|---|
| Records | **1,620** — 863 `recites` / 757 `describes`, measured per record |
| **Permutation control** (#41 lesson) | anchor value vs **another month's** prose for the same topic fires **0.1%**, against a 53.3% true rate → **+53.1 pp lift** |
| Structural alignment | anchor value == terminal point of the anchor channel in **1,620/1,620 (100%)**; `period_end` == the reported month in 100% |
| Series length | **100% exactly 36 points**; 58,320 timesteps, 345,384 datapoints |
| Nulls | **1.12%**, capped at 6 per channel |
| Hygiene | 1,620/1,620 distinct `series_id`, exactly one `<ts></ts>`; strict validate 1,620/1,620, 0 warnings |
| Reconcile | 699 months scanned = 326 usable + 10 no release + 361 no text layer + 2 no table ✓ ; 3,586 topic units = 1,620 emitted + 678 topic not narrated + 672 short window + 486 text under 120 chars + 86 no table row + 39 sparse topic window + 5 failed identity ✓ (the build raises if either side does not balance) |

## How strong is the alignment, exactly

This is the question worth being precise about, because a short snippet next to 216 numbers
invites the answer "not very". Measured on the shipped file:

| | |
|---|---|
| Every figure the text quotes is a value in **this** record's series | **100%** (1,731 figures, 0 orphan) |
| The terminal point is the month the release reports | **100%** |
| The text quotes the terminal value verbatim (`recites`) | **53.3%** — permutation control 0.1% |
| Stated **direction** of this month's move matches the series | **89.5%** (94.3% of `recites`, 82.1% of `describes`) |
| Stated **"N points"** magnitude reproduces from the series | **79.6%** |

The last row deserves its ceiling for context. Recomputing it three ways:

| | |
|---|---|
| first-print[t] − first-print[t−1] — **the series shipped** | **79.6%** |
| the release's own index minus the "previous" it printed — the ceiling | 87.6% |
| today's revised series, both months | **24.1%** |

The 8-point gap to the ceiling is intrinsic to a real-time chain: a release measures its
"N points" against the prior month **as revised by then**, while each point here is that
month's *first* print. The alternative — the revised series — reproduces the same statements
**24.1%** of the time, so the vintage is not a close call.

**What this alignment is not:** the text does not enumerate the series. A record pins a
median of **1 value out of 216**, and 698 of 1,620 pin none — those are the `describes` tier,
where the prose characterises the move ("remained negative but ticked up 1 point", "its
seventh consecutive negative reading") without quoting the level. The window is context for
the terminal, not a transcript, which is the same contract as `11_eia_petroleum_weekly`
(260 weekly points for one week's sentence) and `08_bls_cpi` (120 months) — the ratio just
looks starker here because a per-indicator snippet is short.

### On the 4.5% of texts that are not unique

73 groups of records share their text with a sibling — always **within one release month,
between two indicators the Bank narrated jointly in a clause that names both and quotes
neither's value**. Each carries a **different series**. Joint clauses that *do* quote a
value are cut apart, so "The new orders index rose 9 points to 14.4, and the shipments index
dropped 15 points to -8.7" now becomes two separate, correctly-paired texts. **Cross-month
duplication — the one that would actually be boilerplate reuse — is zero.**

## ⚠️ Where the source actually ends

- **1968-05 … 2001-12 are image scans.** The Bank publishes every release back to the survey's first month, and the build downloads all nine era archives so the wall is *measured*, not assumed: **361 months return zero words** from pdfplumber. That is an OCR-tier problem and is deliberately out of scope. For **1997-09 … 2001-12** the retired site's plain-text version survives in Wayback and is used instead (40 months) — those months feed history into later windows but rarely emit themselves.
- **Ten months have no machine-readable release anywhere**: `1999-05`, `2005-04` (its PDF text layer is corrupted, duplicated glyphs — the #41 WASDE defect), and `2007-05` + `2007-07…12`, which were **never published to the web in any form** (checked against every Wayback capture of all four retired path prefixes). A vintage series cannot be interpolated, so those months stay `null` inside a window.
- **1977-10 and 1993-12** are absent from the Bank's own archive.
- Headroom is thin: the remaining volume is the pre-2002 scan era, which needs OCR.

## Run

```bash
pip install -r requirements.txt          # PyYAML + pdfplumber; + poppler for pdftotext
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=3   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50)
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full (~1,600)
```

A cold full build downloads ~36 MB of era archives plus the Wayback text captures; everything lands in `.cache/` (git-ignored) so re-runs are offline. **Wayback rate-limits** a fast loop — the retired-text months are best prefetched slowly once.

**Output:** `output/philadelphia_mbos_cpt.jsonl` + `output/run_report.json`; `samples/example_output.jsonl` = first 3 records.

**Sibling surveys:** the other Fed regional business surveys follow the same "release narrative + diffusion table" shape and should reuse this package's three extractors — see [fed_surveys_discovery.md](../../docs/fed_surveys_discovery.md). Check the revision question first: if the sibling bank also re-benchmarks its history, its published CSV will disagree with its own prose exactly as Philadelphia's does.

**Sources:** [Philadelphia Fed MBOS](https://www.philadelphiafed.org/surveys-and-data/regional-economic-analysis/manufacturing-business-outlook-survey) — **U.S. public domain**.
