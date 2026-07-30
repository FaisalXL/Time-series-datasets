# Source census — USDA NASS state Crop Progress & Condition

Everything here is counted from the source, not estimated. Two things this package previously
asserted without measuring turned out to be wrong, and both are recorded below so the next pass
doesn't re-derive them: the series-category filter silently dropped a third of the intended
payload, and nine states documented as unbuildable were not.

## 1. Series: Quick Stats bulk file

`qs.crops_20260730.txt.gz` (482 MB gzipped, no API key). 23,846,634 rows scanned;
**1,713,822 STATE-level WEEKLY observations kept** across 5,642 (state, channel) pairs, plus
1,638,689 comparison-mirror observations held separately.

### 1.1 The category filter defect

`STATISTICCAT_DESC` has **52 distinct values** at STATE/WEEKLY. The previous build filtered to
`{PROGRESS, CONDITION, DAYS SUITABLE}`, which excluded this:

| Category | STATE/WEEKLY rows | Status |
|---|--:|---|
| `CONDITION` | 905,332 | used |
| `PROGRESS` | 378,749 | used |
| `MOISTURE` | 369,377 | used |
| `DAYS SUITABLE` | 49,502 | used |
| `DAMAGE` | 4,669 | used |
| `ACTIVITY` | 2,715 | used |
| `HEIGHT, AVG` | 962 | used |
| `SUPPLIES` | 740 | used |
| `NUT SET` | 708 | used |
| `ACCESSIBILITY` | 453 | used |

**`MOISTURE` is the 5th-largest category (369,377 rows) and was not in the filter.** NASS files
soil moisture under its own category, so all eight `SOIL, {TOPSOIL,SUBSOIL} - MOISTURE` channels
matched nothing and were dropped by the builder's silent `all(v is None)` skip. Those are exactly
the channels the README called "the dense backbone … what makes each record a genuinely dense
weekly series rather than a mostly-null per-stage cascade", and the package's claimed
"null fraction 57.8% → 36.2%" improvement rested on them. The committed sample
(`samples/example_output.jsonl`) confirms it: **zero soil-moisture channels, and only
good/excellent of the 5-way condition rating.**

Measured effect of restoring it (Iowa, 2003+): the backbone channels are **98.7% dense**, versus
~26% for the cascading growth-stage channels. The backbone starts 1995-04-09, so it covers the
entire text-available era of every state.

### 1.2 Comparison mirrors — deliberately excluded as channels

| Category | Rows |
|---|--:|
| `CONDITION, PREVIOUS YEAR` | 495,965 |
| `CONDITION, 5 YEAR AVG` | 407,905 |
| `PROGRESS, 5 YEAR AVG` | 325,033 |
| `PROGRESS, PREVIOUS YEAR` | 206,479 |
| `MOISTURE, PREVIOUS YEAR` | 168,210 |
| `DAYS SUITABLE, PREVIOUS YEAR` | 20,812 |

These are real published series and the prose does cite them ("compared with 65 percent last
year"), but they are lagged/averaged restatements of the same measurement, so carrying them as
channels would inflate channel counts with derived duplicates. They are kept in
`.cache/series_mirrors.pkl` and used as a decoy set when measuring alignment — a number in the
prose can match a window value by being last year's figure rather than this week's.

## 2. Text: archived weekly reports

**39,933 real (status-200) archived PDFs across 43 discovery pools**, whole-folder Wayback CDX
listing, four hub-folder spelling variants per state.

Historical reports are **not retrievable from the live NASS site** — spot-checked across the
archive, only the most recent files remain and older URLs return 404, so Wayback is the only route
and its rate limit is the binding cost of the build.

### 2.1 Per state — discovery through shipped records

| State | Archived PDFs | Records | `recites` | % | median null | Channels | Commodities | Weeks/season (2005+) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| FL | 1354 | 1285 | 136 | 11% | 0.38 | 33 | 2 | 52 |
| NE | 3372 | 1224 | 895 | 73% | 0.53 | 84 | 8 | 36 |
| KS | 985 | 846 | 827 | 98% | 0.53 | 83 | 8 | 42 |
| MS | 1179 | 845 | 742 | 88% | 0.47 | 91 | 8 | 38 |
| IA | 1141 | 775 | 741 | 96% | 0.47 | 53 | 5 | 35 |
| AZ | 1225 | 764 | 174 | 23% | 0.42 | 51 | 4 | 38 |
| CA | 1422 | 753 | 0 | 0% | 0.43 | 43 | 3 | 37 |
| OK | 829 | 735 | 102 | 14% | 0.51 | 100 | 8 | 42 |
| AR | 794 | 725 | 594 | 82% | 0.48 | 90 | 8 | 38 |
| ND | 884 | 709 | 614 | 87% | 0.57 | 104 | 8 | 36 |
| LA | 733 | 701 | 638 | 91% | 0.43 | 78 | 8 | 39 |
| SD | 764 | 696 | 447 | 64% | 0.54 | 98 | 8 | 37 |
| WA | 759 | 688 | 151 | 22% | 0.48 | 87 | 8 | 36 |
| DE | 749 | 687 | 165 | 24% | 0.49 | 92 | 8 | 35 |
| MD | 742 | 686 | 265 | 39% | 0.48 | 90 | 8 | 35 |
| CO | 916 | 683 | 28 | 4% | 0.48 | 75 | 8 | 39 |
| UT | 877 | 654 | 54 | 8% | 0.39 | 55 | 6 | 36 |
| PA | 913 | 645 | 273 | 42% | 0.52 | 97 | 8 | 35 |
| SC | 906 | 645 | 480 | 74% | 0.47 | 85 | 7 | 35 |
| VA | 820 | 642 | 15 | 2% | 0.41 | 82 | 8 | 38 |
| TN | 734 | 632 | 551 | 87% | 0.48 | 67 | 6 | 36 |
| NM | 1103 | 621 | 145 | 23% | 0.42 | 78 | 8 | 38 |
| ID | 650 | 610 | 271 | 44% | 0.52 | 86 | 8 | 36 |
| AL | 827 | 599 | 286 | 48% | 0.44 | 64 | 5 | 35 |
| MN | 813 | 592 | 515 | 87% | 0.57 | 85 | 8 | 35 |
| NJ | 884 | 579 | 128 | 22% | 0.52 | 92 | 8 | 35 |
| MT | 904 | 576 | 141 | 24% | 0.54 | 97 | 8 | 39 |
| KY | 674 | 545 | 490 | 90% | 0.48 | 63 | 5 | 36 |
| IN | 728 | 528 | 358 | 68% | 0.46 | 51 | 6 | 36 |
| OR | 554 | 522 | 28 | 5% | 0.41 | 86 | 8 | 35 |
| MO | 741 | 521 | 469 | 90% | 0.51 | 81 | 8 | 36 |
| OH | 668 | 514 | 379 | 74% | 0.51 | 62 | 7 | 37 |
| WY | 910 | 513 | 31 | 6% | 0.50 | 82 | 7 | 38 |
| GA | 603 | 501 | 440 | 88% | 0.49 | 83 | 8 | 36 |
| NY | 980 | 425 | 228 | 54% | 0.39 | 74 | 8 | 34 |
| MI | 443 | 410 | 195 | 48% | 0.50 | 91 | 8 | 36 |
| WI | 1019 | 407 | 336 | 83% | 0.48 | 70 | 7 | 35 |
| IL | 1708 | 296 | 279 | 94% | 0.44 | 57 | 6 | 39 |
| WV | 267 | 230 | 0 | 0% | 0.41 | 62 | 6 | 38 |
| NV | 210 | 210 | 91 | 43% | 0.00 | 14 | 0 | 35 |
| NC | 267 | 198 | 160 | 81% | 0.44 | 78 | 8 | 39 |
| TX | 1903 | 183 | 0 | 0% | 0.64 | 84 | 8 | 46 |

**42 emitting states, 25,600 records.** Median 83.5 channels per state. Season length runs **34
reported weeks (New England) to 52–53 (Florida)** — the fact that fixes the trailing window at 52,
since that is the shortest window spanning a full annual cycle in every state.

The `recites` share varies from **0% (California)** to **98% (Kansas)** and is a real property of
each field office's writing style, not an extraction artifact: reciting states print the rating
percentages as sentences, others narrate qualitatively by region. Tagged per record; see README §4.

## 3. Fetch reconciliation

| Stage | Count |
|---|--:|
| archived PDFs discovered (43 pools, whole-folder CDX) | 39,933 |
| fetched successfully | **39,915** (99.95%) |
| unavailable at source | 16 genuine 404s + 2 non-PDFs |
| parsed to a clean dated weekly report | **33,504** |
| — no parseable week-ending date | 4,926 |
| — no text layer at all (scanned image; OCR-only, out of scope) | 1,292 |
| — corrupt / unreadable PDF | 172 |
| — not a single weekly report (season compilation / monthly) | 20 |
| — garbled text | 1 |
| shipped records | **25,600** |
| — dated to a week with no series | 3,746 |
| — window shorter than 52 reported weeks | 48 |
| — window too sparse (`max_null_frac`) | 68 |
| — text-length gates | 7 |

Every stage balances exactly. The largest recoverable bucket left is the **1,292 scanned-image
reports**, which would need OCR — excluded by the package's standing clean-text-only scope call, not
by a technical limit.
