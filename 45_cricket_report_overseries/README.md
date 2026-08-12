# Cricket Match Reports + Per-Over Ball-by-Ball Series → CPT

> **Status: Built at full scale, schema-v1 clean, quarantined on licence.** One record =
> **one innings** (limited-overs) or **one match** (Tests/multi-day) — the real **ESPNcricinfo
> match report** paired with the **per-over series** it narrates. `text_quality: "real"` always;
> matches with no recap are dropped, never synthesized. **Built: 20,678 records,
> 20,678/20,678 `--strict`, 190 MB, 31,518,835 datapoints, 99.99% distinct texts.**
> Series are at the **source's own granularity — one step per delivery** (`1play`).
> The bank lives in `/data/defu/cpt_corpus/packages/45_cricket_report_overseries/`.
>
> **⚠️ Licence:** the report prose is **copyrighted ESPNcricinfo editorial**, tagged
> `proprietary-review` = *excluded from any release until cleared* (`SCHEMA.md` §6). The
> licence question, and the three options for answering it, are in
> [NOTION_PAGE.md](NOTION_PAGE.md). **Nothing here ships until that is decided.**

**What it is:** the ESPNcricinfo report *narrates the same match the series quantifies* — the
powerplay total, the collapse, when each wicket fell, the run rate — which is exactly what the
per-over channels encode. One of the tightest **`describes`** pairings in the corpus, with a
share of records reaching `recites`.

**The join is exact:** the Cricsheet CSV filename **is** the ESPNcricinfo match id
(`1426312.csv` → match `1426312`), so both halves come from a single integer — no fuzzy matching.

---

## Scale (measured 2026-08-11, not projected)

| | |
|---|--:|
| Cricsheet universe (counted from `all_csv2.zip`) | 22,537 matches → 49,893 innings |
| innings ≥ 12 overs | **47,906** |
| matches carrying a ≥400-char report — stratified sample, then confirmed by the full census | **55.3%** → **53.1%** |
| innings with a report (pre-shape ceiling) | **27,334** (95% CI 25,854–28,814) |
| **records actually built** | **20,678** |

**Why 20,678 and not 27,334** — the ceiling counts *innings* with a report; the hybrid shape
does not emit one record per innings for every format:

| | |
|---|--:|
| innings with a report | 27,334 |
| − TEST/MDM innings (~8,500) collapsed into whole-match records | −8,518 |
| + those matches re-emitted as per-match records | +2,244 |
| − innings the report never narrates (`innings_not_narrated` 586) and short innings (610) | −1,196 |
| **= records** | **20,678** |

The 20,678 is the count that matters; 27,334 remains the right ceiling figure for "how much
paired cricket text exists", and is what supersedes the ~1,900 and ~44k claims.

Two earlier figures were wrong in opposite directions and are superseded: the **~44k** in the
scouting docs assumed near-full report coverage, and the **~1,900** in the 2026-08-11
`REVIEW_STATUS.md` ceiling audit bounded the package to IPL by assumption. Coverage is indeed
"well below 74%" as that audit predicted — it is 55.3% — but 55.3% of a 22,537-match universe
is ~27k, not 1.9k.

**⚠️ Coverage is heavily era-skewed, so a capped run is not a sample.** TEST 2020+ ≈100%, MDM
2015-19 ≈97%, but T20 male 2020+ 39%, T20 female 2020+ 39%, ODI pre-2005 **0%**. `max_records`
walks match ids ascending = **oldest first**, so a capped build over-represents exactly the
strata with the worst coverage.

## Record shape is hybrid, and the split is measured

A report is one document per **match**, but a limited-overs match has two innings — so the
previous build emitted the same prose twice (4× for Tests).

| Format | Shape | Series | Why |
|---|---|---|---|
| T20 · IT20 · ODI · ODM | **one record per innings** | that innings' 4 channels | an LLM assigns each sentence to the innings it narrates; the builder assembles **verbatim from indices**, so `text_quality` stays `real` |
| TEST · MDM | **one record per match** | all innings concatenated + `innings_index` | attribution is *inverted* on these — see below |

Measured on 60 matches — does an innings' attributed passage recite **its own** innings score,
versus **another innings of the same match**:

| | T20 | ODI | ODM | MDM | TEST |
|---|---|---|---|---|---|
| own innings | 23% | 29% | 25% | 20% | **7%** |
| wrong innings | 10% | 0% | 0% | 20% | **20%** |

On Tests the model recites the wrong innings three times as often as the right one. The reports
are not at fault: they are whole-match recaps (`type: "Recap"`, posted d+2/d+3 — i.e. at match
end), so four interleaved innings cannot be separated sentence-by-sentence. Those formats are
built per-match, where the report's unit and the record's unit agree. Full contract:
[`prompts/attribute_v1.md`](prompts/attribute_v1.md).

**The model never sees the time series** — only the innings roster (who batted when), which is
Cricsheet metadata. Showing it the runs and asking which sentences match would score better on
every alignment metric here and would destroy the permutation control that makes them mean
anything.

## Alignment is measured per record, against a permutation control

A record is `recites` only if the prose states **every** innings total its series carries, as an
ordered `(runs, wickets)` pair; otherwise `describes`. Control = the same prose tested against a
**different** match's totals.

| | T20 | ODI | ODM | MDM | TEST | IT20 |
|---|---|---|---|---|---|---|
| n | 13,158 | 2,760 | 2,444 | 1,756 | 488 | 72 |
| true | 89.0% | 86.8% | 92.7% | 77.0% | 69.5% | 80.6% |
| permuted control | 0.6% | 0.6% | 0.5% | 0.0% | 0.0% | 2.8% |
| **lift** | **+88.4pp** | **+86.2pp** | **+92.2pp** | **+77.0pp** | **+69.5pp** | **+77.8pp** |

The control partner is forced to be a **different match**. Records are emitted in match order,
so a per-innings record's neighbour is the *other innings of its own match*, whose total the
shared lede legitimately recites — pairing against it measures the lede, not coincidence, and
reported a 41% control instead of 0.6%.

Two notation traps, both handled: ESPN uses **both** score conventions — English "194 for 4"
(runs first) and Australian "6 for 194" (wickets first, **11%** of team scores) — and bowling
figures ("Lyon 3-48") superficially look like scores, but admitting them injected 8 false
positives per 874 matches, so the hyphen form is excluded.

## The window floor: no longer applicable

Aggregating to overs put a T20 innings at exactly 20 steps, below the 32-step floor used
elsewhere in the corpus, and T20 is 56% of the package — so this package used to swing 3× on
that open decision (**6,885** vs **19,987** records surviving floor 32). Keeping the source's
delivery granularity ends that: **all 20,678 records clear every candidate floor
(12/16/20/24/32).** `#58` is still exposed to the decision — its PSD balance sheets are 3–14
*annual* points with no finer granularity to fall back on, which is the difference between
the two cases.

---

#### 📄 Text — ESPNcricinfo match report

| | |
|---|---|
| **What** | The match report (~850–1,200 words), an editor's over-by-over recap. For per-innings records, the attributed passage plus the match-level lede. `<ts></ts>` is appended directly — nothing generated in between. |
| **Source** | `site.web.api.espn.com/apis/site/v2/sports/cricket/{league}/summary?event={match_id}` → JSON `article.story` (HTML, stripped in-script). |
| **Why this host** | `www.espncricinfo.com` and `hs-consumer-api` 403 bots. `site.api.espn.com` used to work and **began returning 403 (Akamai) for every user agent in August 2026 — a host move, not a bot wall**. `site.web.api.espn.com` serves the identical payload and accepts our identifying research UA; no spoofing. The endpoint resolves purely by `event` = the Cricsheet `match_id` (`{league}` is a required carrier — any valid cricket league id works for any match, all formats). |
| **`text_quality`** | `"real"` — journalist prose, bylines like Sidharth Monga. Matches with no usable recap are **dropped**. |
| **Never use `news[]`** | The same payload's `news[]` array is carrier-league contaminated with generic articles about unrelated matches. Only `article` is match-specific. |

#### 📈 Time series — Cricsheet ball-by-ball (source granularity)

| | |
|---|---|
| **Source** | [Cricsheet](https://cricsheet.org/downloads/) `all_csv2.zip` (**ODC-BY 1.0**), 120 MB. Each match = `{id}.csv` (one row per delivery) + `{id}_info.csv`. **Stdlib only** — `zipfile`/`csv`. |
| **Cadence** | `1play` — one step per delivery bowled, the unit Cricsheet ships. Intra-match game clock, not wall clock. |

| Channel (`unit`) | Meaning |
|---|---|
| `runs_per_ball` | Runs off each delivery (`runs_off_bat + extras`) |
| `wickets_per_ball` | 1 where a delivery took a wicket, else 0 |
| `cumulative_runs` | Running innings total after each delivery |
| `run_rate` | Runs per **over** at that point (`cumulative ÷ (balls/6)`) — kept in overs because that is the unit the reports talk in |

> **Why per delivery.** Cricsheet's archive is one CSV row per ball (`ball` = 0.1, 0.2, …).
> The first version of this builder aggregated 6:1 into overs with
> `over = int(float(ball))` — a transform of our own, not something the source did. Keeping
> the native granularity also dissolved a dependency this package should never have had:
> aggregating put a T20 innings at exactly 20 steps, under the 32-step window floor used
> elsewhere, so the package swung 3× on that open decision. Per-over is still available
> (`shape.series_granularity: per_over`) and re-derivable from the archive at any time.
> ⚠️ Consequence: whole-match Test/MDM records now run **median 1,779 / max 2,695** steps —
> by a wide margin the longest series in the corpus (next is NWPS at 481). Nothing in the
> schema caps length, but `SCHEMA.md` §11 lists it as an open question.

Per-**match** records (TEST/MDM) carry the same channels concatenated across innings, with
`cumulative_runs_in_innings` / `run_rate_in_innings` resetting at each innings boundary plus an
`innings_index` channel marking them.

> **Wicket rule (fixed 2026-08-11).** Cricsheet writes `retired hurt` and `retired not out` in
> `wicket_type`, but they are **not dismissals** — the batter is not out and the scorecard does
> not read "for N+1". Counting them inflated `wickets_per_over` and broke the exact figure the
> report states: a report saying "152 for 8" against a naive count of 9. `retired out` **is** a
> dismissal and stays counted.

---

## Worked example (hand-verified) — IPL 2024 final, SRH innings (`match_id` 1426312)

**📄 Text** · *"KKR's bowlers rip through SRH to win third IPL title"*, Sidharth Monga, 2024-05-27
> *"…SRH were bowled out for the **lowest total in an IPL final, 113**, which KKR chased down
> with 57 balls to spare… Arora went for 17 in the final powerplay over, taking **SRH up to 40
> for 3**… Russell would go on to add a 19th to his tally when ending the innings with Pat
> Cummins' wicket in the 19th over."*

**📈 Series** — the same innings, shown **aggregated to overs** for legibility. Records now
carry the per-delivery form (19 overs = 121 deliveries here); this is that series bucketed by
over, and the two agree on every over-end total.
```json
[
  {"values": [3, 3, 9, 6, 2, 17, 7, 4, 7, 3, 9, 2, 10, 8, 0, 8, 10, 5, 0], "unit": "runs_per_over", "freq": "1over"},
  {"values": [1, 1, 0, 0, 1, 0, 1, 0, 0, 0, 1, 1, 1, 0, 1, 0, 0, 1, 1], "unit": "wickets_per_over", "freq": "1over"},
  {"values": [3, 6, 15, 21, 23, 40, 47, 51, 58, 61, 70, 72, 82, 90, 90, 98, 108, 113, 113], "unit": "cumulative_runs", "freq": "1over"},
  {"values": [3.0, 3.0, 5.0, 5.25, 4.6, 6.67, 6.71, 6.38, 6.44, 6.1, 6.36, 6.0, 6.31, 6.43, 6.0, 6.12, 6.35, 6.28, 5.95], "unit": "run_rate", "freq": "1over"}
]
```

**The alignment is exact:** *"40 for 3"* after the powerplay = `cumulative_runs[5] = 40` with 3
wickets in overs 1–5; *"bowled out for 113"* = `cumulative_runs[-1] = 113` with
`sum(wickets_per_over) = 10`; the powerplay spike *"17"* = `runs_per_over[5] = 17`. Values
re-verified against the current builder on 2026-08-11.

> **Note on the excerpts above:** brief attributed excerpts (© ESPNcricinfo, Sidharth Monga)
> shown only to illustrate the record format, pending the licence decision in
> [NOTION_PAGE.md](NOTION_PAGE.md).

---

## Key issues

- **⚠️ Licence is the one open decision, and it is not ours to self-clear.** Cricsheet is
  ODC-BY 1.0 (fine with attribution). The ESPNcricinfo prose is copyrighted. Note the
  rightsholder is a **single in-house publisher** — `article.source` is empty on all 467 covered
  reports sampled, i.e. staff journalism, **not** AP wire copy. That makes this a materially
  easier ask than `51_espn_us_majors`, whose recaps *are* AP; **do not bundle the two requests.**
- **Schema v1: clean.** Built through `schema/emit.py`; `validate.py --strict` passes with zero
  warnings. The previous builder predated v1 and returned 0/50.
- **`report_posted`, not `report_published`.** `article.published` is a CMS re-stamp that
  disagrees with the real posting date by >30 days in **72%** of articles (one 2018 match is
  stamped `2019-03-18`). The record stores `originallyPosted`.
- **The shared lede is attached to both innings records of a match.** It is a match-level
  sentence reciting both totals; dropping it would remove the package's `recites` evidence and
  leave passages that never say who was playing. **Measured cost: sentence overlap between a
  match's two records is median 0%, mean 5.8%; 99.99% of texts are distinct** — against 2.24×
  duplication in the old shape.
- **`freq: 1over`** is an intra-match cadence — the same game-clock caveat class as other
  intra-day sports candidates.
- **Etiquette:** report fetches are rate-limited and cached per `match_id` under `.cache/espn/`,
  trimmed to the single `article` key the builder reads — which keeps a full-archive cache near
  **200 MB** instead of ~11 GB of untrimmed payloads. HTTP 502 from this host is transient
  throttle, **never** cached as "no article": 76 of 78 resolved on a serial retry.
- **No generated text anywhere in the pipeline.** The LLM returns sentence indices; the builder
  assembles verbatim from the source.

## Run

```bash
pip install -r requirements.txt
export VLLM_KEY=...                                        # attribution endpoint key

python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=5   # smoke test
python scripts/attribute.py                                # LLM pass — cached, resumable
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full build
python ../schema/validate.py output/ --strict              # gate
```

`attribute.py` is resumable and only touches two-innings formats; re-running picks up whatever
is newly fetched. Setting `shape.two_innings_mode: per_match` disables the LLM entirely and
builds the ~12k per-match variant.

> **Full-scale builds do NOT belong in this repo.** This directory holds the builder and a
> capped demo; the bank lives in corpus storage. Use the sanctioned runner, which redirects
> `output_path`/`report_path` into the corpus tree and then gates and records the result:
> ```bash
> python3.11 /data/defu/cpt_corpus/run_full.py 45_cricket_report_overseries
> # -> /data/defu/cpt_corpus/packages/45_cricket_report_overseries/output.jsonl
> ```
> Running `scripts/build_cpt_jsonl.py` directly with `max_records=null` writes the full build
> into `output/` here, which is git-ignored but still the wrong place — it was done that way
> once and 203 MB had to be relocated by hand.

**Output:** `output/cricket_report_overseries_cpt.jsonl` + `output/run_report.json` +
`output/attribution_report.json`; `samples/example_output.jsonl` = first 3 records. `.cache/`
(git-ignored) holds the Cricsheet zip, per-match report JSON, and per-match attribution JSON.

**Sources:** [Cricsheet](https://cricsheet.org/) (ODC-BY 1.0 — *"Downloadable data by Stephen
Rushe, licensed under ODC-BY"*) · ESPNcricinfo match reports via `site.web.api.espn.com`
(**copyrighted — release pending sign-off**, see [NOTION_PAGE.md](NOTION_PAGE.md)).
