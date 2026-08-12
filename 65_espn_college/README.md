# ESPN College Sports Recaps + Score-Progression Series → CPT

> **Status 2026-08-12: full harvest in progress.** Football is complete — **CFB 11,059 records
> from 13,206 games** and FCS building — and Division I basketball, which is 87% of the universe,
> is harvesting now. `validate.py --strict` clean on every shard checked (861/861, 1194/1194, 0
> warnings).
> **Licence-gated: every record is `proprietary-review` (AP wire copy). Not cleared for
> distribution — this is the same rightsholder ask as `51_espn_us_majors`.**

**What it is:** one record = **one finished college game** — the real wire-service recap (served
via ESPN's API) paired with that game's **play-by-play running score** (away/home cumulative, one
point per play). The recap *describes* the shape of the game the series quantifies →
`alignment: describes`.

Sibling of `51_espn_us_majors` (NBA/NFL/NHL). Same endpoints, same play-extraction logic, same
licence question, different and much larger universe: **FBS + FCS football and Division I men's
and women's basketball**.

| | |
|---|---|
| **Universe** | **185,453 finished games**, 2012–2025, censused day by day (not extrapolated) |
| **Records** | CFB **11,059** complete · FCS + basketball in flight |
| **Domain / region** | sports / `US` |
| **License** | ⚠️ `proprietary-review` — AP wire copy via ESPN's API. **Same rightsholder as `51`** |
| **Alignment** | `describes` · `text_quality: real` (verbatim recap; nothing generated, nothing templated) |
| **Series** | 2 channels (away/home cumulative score), `freq: 1play`; football median 185 plays, basketball median ~326 |
| **Built via** | `schema/emit.py` — records are born strict-clean (unlike `51`, whose committed samples are pretty-printed JSON and return 0/2868 under `--strict`) |

---

## The licence gate is the only manual step

**Nothing here needs a key, an account, or authentication.** ESPN's endpoints are public. The only
access requirement is the host: `site.api.espn.com` returns 403 (Akamai) for every user agent,
while `site.web.api.espn.com` serves the identical payload and accepts our identifying research UA
— no spoofing.

What *does* need a human is **AP redistribution clearance (Charon)**. Every record's prose is
Associated Press wire copy, recorded per record in `meta.report_source`. Scoping the ask to **AP**
covers `51_espn_us_majors` (33,960 measured) *and* this package from one rightsholder. If AP
declines, both are zero.

Building ahead of that answer follows corpus precedent rather than departing from it:
`45_cricket_report_overseries` (20,678) and `05_fnspid` (120,522) are both **built, strict-clean,
and deliberately held out of every corpus total**. This package is in the same state.

`45` is a **separate and materially easier** ask (ESPNcricinfo in-house staff journalism). Do not
bundle the two requests.

---

## The measured universe

`census.py --mode walk` walks every day of every season and writes one cell per (league, season),
**refusing to write a season with any unanswered day** — a partial write is indistinguishable
downstream from a real low count.

| league | seasons | finished games | per season | tier |
|---|--:|--:|--:|---|
| **CFB** | 14 | 13,261 | 947 | FBS football |
| **FCS** | 14 | 11,889 | 849 | FCS football |
| **MCB** | 14 | **82,763** | 5,911 | D-I men's basketball |
| **WCB** | 14 | **77,540** | 5,538 | D-I women's basketball |
| | | **185,453** | | |

**The window is 2012–2025 because of recaps, not plays.** `census.py --mode era` over 2006–2025
found a sharp boundary: the 2012 season is the first with wire recaps, in all three sports at
once. 2006–2011 serve full play-by-play (150–350 plays/game) and **no prose whatsoever**, so they
are worth zero records here.

### Yield, measured per season rather than assumed

| league | records / games | yield | note |
|---|--:|--:|---|
| CFB | 11,059 / 13,206 | **83.7%** | 93–97% for 2012–2019; 44% in 2020, 62% in 2021 |
| FCS | in flight | **~55–64%** | lower than CFB because the well-covered games are the FBS matchups |
| MCB | in flight | 2012 = **41.8%** | 2012 is the WORST season in the window, not a typical one — see below |
| WCB | in flight | 2012–13 = **9.5% / 12.6%** | genuine coverage sparsity: 29 of 40 sampled games have no `article` key at all |

**A 40-game sample is not an instrument for this.** At a 13.8% true rate it carries a standard
deviation of 2.35 games — roughly ±6 percentage points — which is why a 40-game probe once read
women's coverage as 25% when the censused answer is 9.5%. Confirmed by 200-draw Monte Carlo against
the full population. The original "~15k" scale claim for this source rested on 40-game samples.

**There is essentially no automated content in football.** CFB's rejections are `no_report` (1,445)
— coverage gaps — against just **91** `source_not_allowed` across 13,206 games. The
"26 of 40 games are Data Skrive" finding that motivated the source allowlist is a **men's
basketball, post-2021** phenomenon and does not generalise to the other tiers. The allowlist stays
on and stays audited per record, but it is not what shapes football's yield.

### Two different constraints bind in different eras

For men's basketball 2012, **1,897 of 5,825 games (32.6%) carry a usable ≥400-char AP recap and
have ZERO plays in the payload** — the text exists and the series does not, so they cannot become
records here. Nothing recovers them: the only other numeric structure available is period
linescores, which is 2 points for a game of halves, far under any window floor and already rejected
in `51` for football at 3–4 points.

That constraint is era-specific and it inverts. 30-game samples per season (±8 points, so
directional only) put zero-play games at 12/30 in 2012, 4/30 in 2016, 3/30 in 2019 and **0/30 in
2022 and 2025**, while recap coverage moves the other way — 22/30 in 2012 rising to 29/30 in 2016,
then falling to 18/30 in 2022. Usable-on-both-counts runs 47% (2012), 87% (2016), 87% (2019), 60%
(2022), 80% (2025).

**So the first MCB shard is the floor, not the average**, and the two halves of a record fail for
opposite reasons at opposite ends of the window. Per-season truth comes from the harvest's own skip
counts, which separate `no_report` from `short_game` precisely so this stays visible.

⚠️ **This file states yields it has measured and leaves the rest blank.** Multiplying a per-unit
rate into a federation total is exactly how this tier has been wrong before — 14× down on cricket,
17% up on `51`, and 15× down on `61`.

---

## Alignment: `describes`, and the §7 test is run rather than asserted

SCHEMA.md §7 qualifies `describes` **"if the description is specific to *this* series (not
boilerplate)"**. That is a testable condition, and these recaps make it easy to test: they state
the final score, which is literally the last value of both channels.

So `verify_alignment.py` checks two anchors against a **permutation control** — the same prose
paired with a *different* game's series values, same league, same era, same house style — because a
match rate without a measured coincidence floor means nothing (the lesson from `61`). Scores are
small integers and recaps are full of them, so the floor is not zero by assumption.

| tier | final score in own prose | control | lift | halftime anchor | control |
|---|--:|--:|--:|--:|--:|
| CFB | 0.9853 | 0.0058 | **169×** | 0.227 | 0.054 |
| FCS | 0.9860 | 0.0038 | **261×** | 0.159 | 0.041 |
| MCB (demo) | 1.000 | 0.000 | — | 0.650 | 0.000 |
| WCB (demo) | 1.000 | 0.025 | 40× | 0.350 | 0.000 |

Across **all 12,699 records harvested so far, 0.98724 state their own final score** — a census, not
a sample, because the anchor is just a regex. The halftime score is a genuinely independent second
anchor, recoverable from the series only because `meta.period_end_idx` records where each period
ends.

**The tag stays `describes`, deliberately.** Only the terminal value is quoted, not the 160–330
point path, so `recites` would overclaim under §7. The anchor is *verification of the pairing*, not
grounds for promoting the tier.

### Shared recaps are resolved, not just counted

Text distinctness is **99.992%**, and the one collision is worth knowing about: ESPN serves the
*same* recap for events 400548101 (Eastern Michigan at Florida, 2014, 0–65) and 401752668 (LIU at
Florida, 2025, 0–55). Different teams, dates and scores — so at most one of those records has prose
describing its own series, and the other is a mispairing that **every id-level check passes
cleanly**. `aggregate.py` resolves such a group to the record whose own final score appears in the
text and writes the loser to `output/exclusions.json` with its reason; a group where nobody
verifies, or several do, is excluded whole rather than guessed at. (`45` carried 2.24× text
duplication with every id distinct — which is why distinctness is checked on a text hash
independently of `series_id`.)

---

## Series health: the cumulative score was not cumulative

ESPN populates `awayScore`/`homeScore` on every play but is **not reliable about it**.
Administrative plays carry a stale pre-scoring value — event 401677077 has a timeout immediately
after a touchdown reporting the pre-TD score — and some carry a value from an unrelated point in
the game entirely: event 401706896 reports `a=57 h=86` inside a `68/92` neighbourhood. Raw, that
produced a **decreasing "cumulative" score on 9 of the first 50 records**. Plays are correctly
ordered and unique (verified against `sequenceNumber`), so this is a field-level defect, not a
parsing bug.

A cumulative score cannot decrease, so each channel carries a **running max**, and
`meta.score_fix_plays` records how many plays needed it. **12,699/12,699 records are healthy.**

Guards, each set from a measured distribution rather than a round number:

- **`max_score_fix_frac: 0.25`** — the fix-fraction is p50 0.005, p90 0.040, p99 0.155, then a thin
  tail to 0.95. That tail is real: a game reporting 95% of plays below the carried score has a
  mostly-garbage score field, and monotonising it yields a staircase that reaches the final early
  and sits there, so the *path* is wrong even where the endpoint matches. 0.25 sits above p99 and
  costs 0.63%.
- **`max_plays_by_sport`** — football is p99.9 = 267 plays, and one payload in 5,241 carries
  **1,493**, of which 1,346 are stamped period 3 (event 400756912). Caps: football 500,
  basketball 1,200 against a measured legitimate max of 782 from multi-overtime games.
- **The official-final check** remains the principled guard and dropped 156 CFB games: a wrong
  score value that survives monotonisation lands on the last point and fails there.

Two things that are **not** defects, both of which were initially flagged as such:

- **A flat channel is a shutout.** 156 records were reported as broken series; every one was a real
  45–0 or 49–0 game whose away channel legitimately never moves. The check now fires only when
  *neither* side ever scored.
- **Non-monotone `sequenceNumber` is a numbering convention, not corruption.** It looked like a
  clean integrity signal, but 2,002 of 5,141 cached football payloads (39%) are non-monotone,
  concentrated in pre-2010 ids. Rejected as a guard.

---

## ⚠️ Four discovery traps, all found by measurement, all silent at HTTP 200

Full matrix, completed games returned for one date per league:

| league | correct params | the trap |
|---|---|---|
| **CFB** | **no `limit` at all** → 52 | `&limit=1000` → **25**. A 52% undercount. |
| **FCS** | `&groups=81` → 60 | bare returns the FBS slate; FCS is invisible without it |
| **MCB** | `&groups=50` → 155 | bare → 21. A **7×** undercount. |
| **WCB** | `&groups=50` → 116 | bare → 7. A **16×** undercount. |

1. **`&limit=1000` must NOT be sent.** It is the correct fix for the monthly *range* queries `51`
   uses, and it **truncates** a single-date college-football query. It was hardcoded into the
   shared `scoreboard_url` here, so every CFB discovery call this package originally made lost half
   the slate — the source of the "exactly 25 games on an October Saturday" that read as suspicious.
   Params are per league for exactly this reason.
2. **`&groups=81` is a separate tier, not a filter.** `bare` and `&groups=80` return the *identical*
   52 event ids, so the default slate is FBS. `groups=81` returns ~849 games/season of which ~23%
   overlap, and they carry the same journalism (13 of 14 sampled have AP recaps, median ~2,300
   chars). `exclude_overlap_with` is **required**, not optional: `series_id` is
   `espn_<league_slug>_<event_id>` and does not encode the tier, so a shared game harvested under
   both labels is two records claiming to be the same series.
3. **College basketball rejects date *ranges*** (404 on every monthly range tried) while single
   dates work, so there is no range-query shortcut — the opposite of `51`. Discovery is one request
   per day per league, which is why the census is cached and reused via `discovery.census_seasons`.
4. **The FCS 2020 season was played in spring 2021.** Under a standard Aug 15 → Jan 20 football
   window the walk returned **52 games over 18 active days** against ~890 in neighbouring seasons.
   That is the dangerous kind of wrong number — 52 reads as a plausible COVID collapse. Most FCS
   conferences did not play in autumn 2020 at all. `SEASON_WINDOW_OVERRIDES` extends football 2020
   to June 30 2021, recovering **311 FCS games (6×)** and 628 CFB games.

---

## Record shape

```json
{
  "text": "<verbatim AP recap>\n\n<ts></ts>",
  "timeseries": [
    {"values": [0, 0, 3, 3, ...], "unit": "away_score_cumulative", "freq": "1play"},
    {"values": [0, 2, 2, 5, ...], "unit": "home_score_cumulative", "freq": "1play"}
  ],
  "alignment": "describes", "license": "proprietary-review",
  "text_source": "third_party", "text_quality": "real",
  "domain": "sports", "region": "US",
  "meta": {"league": "MCB", "report_source": "AP", "n_plays": 326,
           "period_end_idx": [162, 325], "score_fix_plays": 0, "espn_season_year": 2025, ...}
}
```

Every play carries a point, not only scoring plays — the flat stretch of a possession that ends
without points is part of what the recap describes. Per `51`'s finding, period-level granularity
gives only 3–4 points for football and was rejected.

`meta.espn_season_year` is ESPN's own field and **does not match this package's season labels**:
ESPN labels a basketball season by its *ending* year (a 2024-12 game is season 2025) while
`census.py` labels seasons by start year. Kept under the source's name so the two conventions are
not conflated.

**A game is dropped, never patched, when:** it has no recap ≥400 chars · its source is outside the
allowlist · it has <20 plays or <2 periods · its play count is implausible for the sport · more
than 25% of its plays needed the monotonic correction · **or the play-by-play final disagrees with
the official final**.

---

## Pipeline

```bash
pip install -r requirements.txt

# 1. what era is usable at all (cheap: ~660 requests for 20 seasons x 3 leagues)
python scripts/census.py --mode era --seasons 2006:2025

# 2. the real census -- one cell per (league, season), resumable, refuses partial writes
python scripts/census.py --mode walk --seasons 2012:2025 --workers 6

# 3. full harvest, one shard per (league, season), resumable
python scripts/harvest.py --seasons 2012:2025 --workers 8 --delay 0.15

# 4. aggregate: counts, series_id uniqueness, health on every record, the §7 control
python scripts/aggregate.py
python ../schema/validate.py --strict output/shards/CFB_2012.jsonl

# capped demo for review (60 total / 20 per league, all four tiers exercised)
python scripts/build_cpt_jsonl.py
```

**Measured throughput.** Concurrency is a global rate limit — all workers share one `Fetcher` whose
gap is held under a lock — so the aggregate rate is 1/gap regardless of worker count; workers only
hide per-request latency, which is itself worth ~2× (a serial walk achieves ~1 req/s against a
0.45s gap). Two processes at 8 workers and `gap=0.15` sustain **13.6 req/s with zero throttles**.
Three processes reached ~10 req/s and drew throttles, so ESPN's tolerance is real but finite; the
AIMD backoff widens the gap on 502s, which is what ESPN returns instead of 429. Cache footprint is
~68 GB for the full universe (football 507 KB/game, basketball ~325 KB).

## Not included, and why — both measured, not assumed

- **ESPN MLB (~17.2k claimed) → ~900 real.** Recap coverage **2.5%** (1 of 40): the summary payload
  has **no `article` key at all** on 39 of 40 games. Its series half is the richest of any league
  checked (413 plays, 69 win-probability points) — the text is what is missing, backwards from the
  claim. MLB is a *US major* and belongs as a fourth league in `51_espn_us_majors`, not here.
- **ESPN soccer (~8k claimed) → 0, on two independent grounds.** (a) **No numeric series**: soccer
  summaries carry `commentary` (text) and `keyEvents` (20–27, of which goals are 2–6), so a running
  scoreline is 2–6 points. (b) **Different rightsholders**: Reuters 40/40 for EPL and UCL, Field
  Level Media 35/40 for MLS — two or three *new* licence asks, making the AP request harder rather
  than wider.

## Open items

- **AP licence** is the only thing between this and a shippable package. Same ask as `51`.
- **SFT-overlap check (SCHEMA §8 item 5) has not been run.** Required before banking.
- **Basketball yield is unmeasured** and is 87% of the universe. It will come from the harvest's own
  per-shard source counts, not from a sample.
- **The CFB/FCS 2020 shards need rebuilding** under the corrected spring-2021 window; the cells are
  already re-walked.
- **D-II/D-III basketball and lower football divisions are untested.** `groups=81` proved that a
  group id can hide a whole populated tier, so "no other tiers exist" should be measured, not
  assumed.
