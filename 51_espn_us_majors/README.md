# ESPN US Majors (NBA + NFL + NHL) Recaps + Score-Progression Series → CPT

> **Status: Built at full scale, 2026-08-13 — 39,525 records, 39,525/39,525 `validate.py --strict`,
> 0 warnings.** One record = **one finished game**: the real Associated Press wire recap (served via
> ESPN's API) paired with that game's **play-by-play running score** (away/home cumulative, one
> point per play). `text_quality: "real"` always; games with no usable recap are dropped, never
> synthesized.
>
> **⚠️ License: UNRESOLVED, and this package is HELD.** The recap prose is AP wire copy — a
> different and likely stricter copyright chain than [`45_cricket_report_overseries`](../45_cricket_report_overseries/)
> (ESPNcricinfo's own staff journalists). Every record is tagged `proprietary-review`,
> `output/shards/` is gitignored, and **no rows are added to `cpt_corpus/manifest.jsonl`**.
> Precedent: `45_cricket` (20,678) and `05_fnspid` (120,522) are built, strict-clean and held.
> Scope the ask to AP and it covers this package and [`65_espn_college`](../65_espn_college/)
> (73,347) together — **112,872 records behind one licence question.**

**What it is:** the AP recap narrates the game's *shape* — a comeback, a blowout, a lead held
throughout — which is what the cumulative score channels encode. That makes this a genuine
`describes` pairing: an independent news account lined up against the numbers it narrates. The
claim is **tested, not asserted** — see [Alignment](#alignment-is-measured-against-a-control).

---

## The numbers

| | records | timesteps | datapoints |
|---|--:|--:|--:|
| NBA | 18,302 | 8,484,370 | 16,968,740 |
| NFL | 4,261 | 747,984 | 1,495,968 |
| NHL | 16,962 | 5,315,173 | 10,630,346 |
| **Total** | **39,525** | **14,547,527** | **29,095,054** |

45 shards, one per (league, calendar year), 2012–2026. `series_id` unique across all shards
(0 duplicates), **39,525 distinct texts out of 39,525 records** (no shared recaps at all), and
**series health passes on every record, not a sample**: 0 unhealthy.

---

#### 📄 Text — AP recap via ESPN's API

| | |
|---|---|
| **What** | The game recap. `text` = the cleaned recap with `<ts></ts>` appended directly — no generated or templated framing sentence. |
| **Source** | `site.web.api.espn.com/.../summary?event={event_id}` → JSON `article.story` (HTML, stripped), plus `headline` / `source` / `published`. |
| **`text_source`** | `"third_party"` — independent AP journalism about the game, not an official team/league statement. |
| **`text_quality`** | `"real"`. Games with no usable recap are **dropped**. |
| **Length** | median **3,897 chars / 973 tokens**. See [Token length](#token-length-and-what-truncation-costs). |

#### 📈 Time series — play-by-play running score

| | |
|---|---|
| **What** | 2 channels, **one step per play** — median 360 steps per game. |
| **Source** | ESPN's own play-by-play, from the same `summary` response as the recap — no second fetch. NBA/NHL expose a flat `plays[]`; **NFL nests plays under `drives.previous[].plays[]`**, handled by `flatten_plays()`. |
| **Cadence** | `1play` — a domain-native epoch. One step = one play, not wall-clock time. |

| Channel (`unit`) | Meaning |
|---|---|
| `away_score_cumulative` | Away team's running score after each play |
| `home_score_cumulative` | Home team's running score after each play |

---

## Alignment is measured against a control

`describes` is a weak claim on its face. But these recaps contain a hard, checkable anchor: they
state the final score, and the final score is literally the last value of both channels. Two
anchors are tested per record, each against a **permutation control** that pairs this record's
prose with a *different game's* values — same league, same era, same house style, different game.

| league | states own final | control | teams named | control | **joint** | control | lift |
|---|--:|--:|--:|--:|--:|--:|--:|
| NBA | 0.9952 | 0.0020 | 0.9990 | 0.0103 | **0.9942** | **0.000000** | 0 control hits in 15,985 trials |
| NFL | 0.9928 | 0.0072 | 0.9832 | 0.0135 | **0.9762** | 0.000570 | **1,712×** |
| NHL | 0.9970 | 0.1407 | 0.9985 | 0.0142 | **0.9958** | 0.003131 | **318×** |

Over **every** record, not the sample: **0.99603** state their own final score, **0.99362** state it
*and* name both teams.

**The joint test exists because the score anchor alone is not equally strong per league — and only
the control revealed it.** NHL's control sits at **14%**: hockey finals occupy just **39 distinct
score pairs** across 20,000 games ((2,3) alone is 15% of them), so a random other game's final
appears in a given recap by coincidence one time in seven. That is a 7× lift, against NBA's 497×.
Adding the entity anchor — the prose must also name both team nicknames, which are high-entropy —
takes NHL to 318× and NBA to a control that never fires once.

A related defect the control also exposed: the pair regex was matching **inside won-loss records**.
`the Panthers (23-12-4)` yielded (12,23), `improved to 6-1-1` yielded (1,6), `a 14-3-0 run` yielded
(3,14). Hockey recaps are full of these. Matches embedded in a longer dash-run are now rejected —
they were never scorelines. That alone moved NHL's control from 0.1554 to 0.1412; the remaining
floor is the small value space, which is why the entity anchor was needed.

**The tag stays `describes`, not `recites`.** Only the terminal value is quoted, not the 360-point
path — `recites` would overclaim (SCHEMA.md §7). `meta.period_end_idx` is stored so the score at
the end of any period is recoverable as a second, independent anchor.

---

## What was measured, and what it retracted

Every number below replaces an assumption this README previously stated.

### 1. The universe started in 2014 for no measured reason

Recap coverage does not fade in — it **switches on**, in all three leagues, in the 2012-13 season.
Measured month by month across the boundary, 10 games sampled per month:

| league | before | **switch-on** | after |
|---|---|---|---|
| NBA | 2012-03 1/10 · 2012-04 0/10 | **2012-11 10/10** | 2012-12 10/10 · 2013-01 10/10 |
| NFL | 2011-11 1/10 · 2011-12 1/10 | **2012-09 10/10** | 2012-10 10/10 · 2012-11 10/10 |
| NHL | 2012-03 0/10 · 2012-04 1/10 | **2013-01 10/10** | 2013-02 10/10 · 2013-03 10/10 |

(NHL 2012-11/12 are empty: the 2012-13 lockout season did not start until 2013-01-19.)

**Play-by-play does not bound this package — recaps do.** 2002 payloads carry 300–450 plays in all
three leagues and no prose at all. This is the same boundary `65_espn_college` measured for college
sports, found independently here.

> **RETRACTED:** discovery previously started at 2014-07 with no recorded justification, costing
> the 2012-13 and 2013-14 seasons of all three leagues. The census window is now 2012–2026.

**The pre-2012 tail was harvested rather than assumed away**, into `output/shards_pre2012/`:
2005–2011 across all three leagues yields a rounding error — NBA 2005 gave **1 record from 1,442
games**, NBA 2006 **0 from 1,452**. It is excluded on that measurement, not on a guess.

### 2. `article.source` is not always `"AP"`, and the common spelling is the other one

**Counted over all 37,089 cached summary payloads — a census, not a sample:**

| league | AP | Associated Press | total AP | no `article` key | ESPN desk | coverage |
|---|--:|--:|--:|--:|--:|--:|
| NBA | 7,694 | 7,098 | 14,792 / 15,845 | 935 | 116 | **93.35%** |
| NFL | 1,735 | 2,072 | 3,807 / 4,075 | 241 | 26 | **93.42%** |
| NHL | 7,386 | 8,021 | 15,407 / 17,169 | 1,728 | 31 | **89.74%** |
| **all** | 16,815 | 17,191 | **34,006 / 37,089** | 2,904 | 173 | **91.69%** |

> **RETRACTED:** "`article.source == "AP"` on every game checked across all 3 leagues" was wrong in
> two ways. **Both spellings are in the feed and `"Associated Press"` is the more common one** —
> an allowlist of just `["AP"]` would have silently halved this package. And the field is not
> always AP: 2,904 games carry no `article` key at all, plus 173 ESPN desk stories under four
> spellings, one `"Sportradar"` and one blank.

The 92.6% figure from the old 421-game stratified sample was *right* — the full count is 91.69%.
It is now a census rather than an estimate.

### 3. `&limit=1000` is required here and forbidden in the sibling package

Measured by comparing event-id **SETS** against a full day-by-day walk of the same month:

| league | range bare | range `&limit=1000` | day walk |
|---|--:|--:|--:|
| NBA 2024-01 | **100** | **231** | 231 |
| NFL 2023-11 | 59 | 59 | 59 |
| NHL 2024-01 | 208 | 208 | 208 |

With the limit, the monthly range reproduces the day walk **exactly** — set equality, not matching
counts. Without it NBA silently drops 131 of 231 games at HTTP 200. The cut is at 100 raw events
and is **per league** (NHL returned 209 raw with no param at all), and the payload carries no
`count` / `pageCount` / `pageSize` field, so **nothing in the response announces the truncation**.

This is the **opposite** of `65_espn_college`, where the same param *truncates* a single-date query
(52 games bare, 25 with the limit). Same endpoint, same param, opposite correct answer, because one
package queries ranges and the other queries days. **The params are therefore part of the cache
key** — keyed on the date alone, the fix silently reads the truncated payload back out of cache.

### 4. The census cell is a calendar year, not a season

Months tile the calendar exactly — no gap, no overlap, no window to get wrong — and a monthly range
costs one request whether the month holds 0 games or 250. That matters in this era specifically:
**COVID moved two seasons clean out of any fixed window** (the 2019-20 NBA season finished
2020-10-11; the NHL played its 2020-21 season Jan–Jul 2021), and the 2012-13 NHL season did not
start until 2013-01-19. Each record still carries ESPN's own `season.year` and `season.type`
(1 preseason / 2 regular / 3 postseason / 4-5 all-star), so season slicing survives in the data.

### 5. The source URL form was wrong

`https://www.espn.com/basketball/game/_/gameId/401810333` → **404**.
`https://www.espn.com/nba/game/_/gameId/401810333` → **200**.

> **RETRACTED:** `source` was built from the *sport*, which does not resolve. It is built from the
> **league slug** now, verified 200 for all three leagues. ⚠️ `65_espn_college` builds the same
> field the same wrong way and its 73,347 records carry non-resolving `source` URLs.

### 6. Two defects predating schema v1, both fixed

1. **`samples/example_output.jsonl` was a pretty-printed JSON array.** `validate.py --strict` reads
   JSONL, so it saw **2,868 malformed records and failed every one** — a package reporting itself
   as built was failing the gate 100%. Everything is emitted through `schema/emit.py` now, so
   records are *born* strict-clean.
2. **The builder accumulated every record in memory before writing.** At this scale that is ~1 GB
   resident and one monolithic file that restarts from zero if interrupted. `harvest.py` shards
   per (league, year), resumable, with the report written **last** as the completion marker.

---

## Series reconstruction: two hard constraints, both audited

ESPN's per-play `awayScore`/`homeScore` are unreliable, and **the errors go in both directions**.
Traced on NFL event 401123243:

```
178  P4  a=14  Passing Touchdown ... extra point is GOOD     <- true score, 14
179  P4  a=16  Kickoff                                       <- spurious HIGH
180  P4  a=14  Penalty (False Start) - No Play               <- back to 14
```

Two properties of what a cumulative score *is* are applied, in order:

1. **It cannot exceed the game's final score.** A raw value above the official final is impossible,
   so it is ignored. Counted per record as `meta.score_clamp_plays`.
2. **It cannot decrease.** Each channel then carries a running max. Counted as
   `meta.score_fix_plays`.

Neither is a heuristic fitted to this feed. Constraint 1 is new here and it is load-bearing: on NFL
2019 it **recovered 11 of 11 games** that were being dropped as `score_mismatch`, moving that
shard's yield from 0.946 to 0.979 — and it cannot weaken the guard, because the official-final
check still runs afterwards and clamping can only cap an overshoot, never raise a series that fell
short.

**`max_score_fix_frac` is deliberately OFF, and that is a measured decision.** `65` cuts at 0.25;
here 249 records (0.63%) sit above it, and they were **inspected rather than cut**. Worked example,
NHL event 401272664, the worst in the corpus at 251 of 260 plays "fixed": the raw pairs are
`(1,0)×94, (2,0)×80, (3,0)×58, (3,1)×16` — **ESPN's NHL feed reports the acting team's score and
zeroes the other channel on most plays.** The running max reconstructs the game exactly: away
scores at plays 7/101/182, home at 6/240, final 3-2 matching the official boxscore, with only 30%
of the series sitting at the final value. A high fix fraction here is a feed *convention*, not
corruption. Cutting at 0.25 would have discarded 249 correct records — the [`#55`
mistake](../55_noaa_stock_assessments/) exactly.

**`max_score_clamp_frac: 0.5` IS on, and both sides of the cut were inspected.** Of 160 records
carrying any clamp, p90 is 0.016 — isolated bad plays, the intended target. Then a gap:

- **KEEP** `401272400` at 0.21 — away steps 1,2,3 and home 1,2,3,4, every goal its own step.
  *"Giroux, Flyers top Rangers 4-3 despite Kreider hat trick."* Path is perfect.
- **DROP** `401350290` at 0.824 — home jumps 3 → 5 in a single step.
- **DROP** `401298993` at 0.886 — home jumps 6 → 9 in a single step.

In both dropped cases the **endpoint still matches**, so the official-final check cannot catch them:
what is wrong is the middle of the path, which is exactly what the `describes` claim rests on.
The cut costs 2 records.

A flat channel is a **shutout, not a defect** — only a game where *neither* side scored is flagged.

---

## Skip reasons, reported separately

44,988 games walked → 39,525 records. These are the numbers the package is judged on, and
"no recap exists" is a coverage gap while "this recap is not AP" is a licence decision:

| reason | games | what it means |
|---|--:|---|
| `no_report` | 4,402 | no `article` key, or a story under 400 chars — **coverage gap** |
| `score_mismatch` | 513 | play-by-play never reaches the official final — plays missing from the feed |
| `source_not_allowed` | 331 | not AP: 301 + 26 + 2 + 1 ESPN desk copy under four spellings, 1 Sportradar |
| `short_game` | 215 | under 3 periods or 20 plays |
| `unreliable_score_path` | 2 | clamp fraction above 0.5 — see above |
| `implausible_play_count` | 0 | the per-sport guard never fired |

Yield by league: NBA 0.899 · NFL 0.920 · NHL 0.848.

---

## Token length, and what truncation costs

**These recaps are long — 2.7× the college package's median.** Measured with `cl100k_base` on a
9,000-record stratified sample:

| | NBA | NFL | NHL | **all** |
|---|--:|--:|--:|--:|
| median | 939 | 1,070 | 889 | **973** |
| p90 | 1,145 | 1,266 | 1,115 | 1,194 |
| max | 2,039 | 1,950 | 1,760 | 2,039 |
| share over 500 | 90.3% | 99.1% | 88.3% | **92.6%** |
| share over 1,024 | 28.5% | 63.1% | 22.2% | 37.9% |
| share over 2,048 | 0.0% | 0.0% | 0.0% | **0.0%** |

For comparison, `65_espn_college`'s median is 357 tokens — its recaps are genuinely shorter
(median 1,181 chars vs 3,897 here), so **the college package's token profile does not transfer.**

### A 500-token truncation still costs essentially nothing

| | NBA | NFL | NHL |
|---|--:|--:|--:|
| final-score anchor in the **first 500 tokens** | 0.9977 | 0.9923 | 0.9970 |
| records that lose the anchor | 7 | 23 | 9 |
| anchor already in the **first paragraph** | 0.2787 | 0.1347 | 0.3583 |

**The conclusion from `65` holds, but its stated reason does not.** That package's explanation was
"AP puts the score in the lede" — measured at 0.7422 there. Here the lede is a *narrative hook*
(`"TORONTO -- Kawhi Leonard was so good Tuesday night, Raptors coach Nick Nurse just wanted to
enjoy the show."`) and the score lands in the second or third paragraph: first-paragraph rates are
only 0.13–0.36. The score still arrives well inside 500 tokens, so plain truncation is safe — but
it is safe for a different reason than the sibling package records. Record
`meta.text_truncated_at` if you apply one.

This is the opposite of `61_ons_statistical_bulletins`, where a token cap orphaned recited values
in 92% of records and forced a split-don't-cut rule.

**An LLM summarisation pass is rejected**, on the same grounds as `65`: it would force
`text_quality: generated`, which SCHEMA.md §7 permits only as a tagged minority with sign-off — the
gate already holding `05_fnspid`'s 120,522 records. It would add a second blocker to a package that
has one. And it destroys what makes the package qualify: `describes` survives because AP wrote
prose that independently describes this game, which the control measures at 318× to unbounded. A
summary is text derived from a series we already hold.

---

## Known source artifact, deliberately not "fixed"

ESPN's own payload ships the AP dateline as `"OKLAHOMA CITY -- — Eric Gordon scored 25 points..."`
— a double dash *and* an em-dash, in the raw `article.story` before any processing here. It appears
in ~46% of stories. It is **left byte-identical to the wire**: normalising it in this package but
not in `65_espn_college` (already built, 73,347 records) would make two siblings from one pipeline
disagree on text, and editing wire copy whose redistribution is unresolved is not a call to make
silently. Flagged here rather than patched; it is a one-line change applied to both if wanted.

---

## Run

```bash
pip install -r requirements.txt

python scripts/census.py --mode walk --years 2005:2026 --workers 6   # ~790 requests, resumable
python scripts/harvest.py --years 2012:2026 --workers 8 --delay 0.15 # sharded, resumable
./scripts/status.sh                                                  # progress, any time
./scripts/finalize.sh                                                # aggregate + strict gate
python scripts/token_stats.py                                        # token + truncation cost
python scripts/corpus_totals.py                                      # records/timesteps/datapoints
```

**Throughput:** two processes × 8 workers at `gap=0.15` sustains ~13 games/s with zero throttles.
Three processes drew throttles at a *lower* aggregate, so burstiness matters more than the mean.
All workers share one `Fetcher` whose inter-request gap is held under a lock, so concurrency is a
**global** rate limit, not N independent ones. ESPN throttles with **HTTP 502, not 429**, and a
failed fetch is **never cached** — a cached throttle failure looks like a measurement forever after.

**Output:** `output/shards/{LEAGUE}_{YEAR}.jsonl` (gitignored — real AP prose at scale) plus a
`.report.json` per shard, `output/harvest_summary.json`, `output/token_stats.json`, and
`output/finalize.out`. `samples/example_output.jsonl` is a 5-record JSONL demo for review.

**Environment:** stdlib + PyYAML for the core; `tiktoken` only for `token_stats.py`. Python 3.11
(`/usr/local/anaconda3/bin/python3.11`).

**Sources:** `site.web.api.espn.com` — recap text is Associated Press wire copy (**copyrighted;
redistribution unresolved**); play-by-play and boxscore data are ESPN's own.
`site.api.espn.com` returns 403 (Akamai) for every UA including browser ones.
