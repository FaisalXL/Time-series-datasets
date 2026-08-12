# Cricket innings-attribution prompt, v1

Splits one ESPNcricinfo match report into the passages that narrate each **innings**, so a
limited-overs match yields one record per innings instead of two records sharing identical
prose. Applied **only** to two-innings formats (`T20`, `IT20`, `ODI`, `ODM`). Four-innings
formats (`TEST`, `MDM`) are built per-match and never go through this pass — see
*Why not Tests* below.

## The contract

1. **The model returns sentence INDICES, never text.** The builder assembles the record
   verbatim from the source, so `text_quality` stays `real`. This matters because the frozen
   v1 vocab is `TEXT_QUALITY = {real, generated}` — there is no `llm_summarized` slot, and
   `SCHEMA.md` §7 allows `generated` only as "a small, tagged minority" with sign-off.
   Extraction is the only route to a per-innings text that does not put this package into
   that bucket. (Same reasoning as `05_fnspid/prompts/extract_v2.md`.)

2. **The model never sees the time series.** It sees the innings roster — who batted in which
   innings, and how many overs — which is Cricsheet *metadata*, not the series. Showing it
   `runs_per_over` and asking which sentences match would score better on every alignment
   metric we have, and would destroy the permutation control that makes those metrics mean
   anything. The model splits on narrative structure alone; whether the result aligns with
   the series is then something we can honestly measure afterwards.

3. **The model ranks; the builder budgets.** Indices come back ordered by importance within
   each bucket. The builder assembles in **document order** (a cricket innings is a
   chronological narrative and the series is chronological too), but when a record exceeds
   the token budget it drops the model's lowest-ranked sentences rather than tail-cutting.
   Nothing is cut mid-word and nothing arbitrary is discarded.

4. **The `shared` bucket is attached to every innings record of that match.** The lede of a
   cricket report — "Titans 194 for 4 (Kuhn 83*) beat Lions 152 for 8 by 42 runs" — is a
   match-level sentence that recites *both* innings' totals, and it is the single most
   information-dense sentence in the report. Dropping it would strip the package of its only
   `recites`-tier evidence and leave spans that do not say who was playing; a CPT record is
   supposed to be a self-contained statement about the world. Attaching it costs a measured
   ~2 sentences of overlap between the two records of a match, which the run report states
   explicitly rather than claiming zero reuse.

## Why not Tests and multi-day matches

Measured on 60 matches before this pass was written:

| | T20 | ODI | ODM | MDM | TEST |
|---|---|---|---|---|---|
| span recites its **own** innings score | 23% | 29% | 25% | 20% | **7%** |
| span recites **another** innings' score | 10% | 0% | 0% | 20% | **20%** |

On Tests the control is **inverted** — attribution recites the wrong innings three times as
often as the right one — and MDM sits at chance. Duplicate index assignments also rise on
four-innings formats. The reports themselves are not the problem: they are whole-match
recaps (`type: "Recap"`, posted d+2/d+3 of the match, i.e. at its end), so the model is
failing to separate four interleaved innings, not correctly declining to narrate unplayed
ones. Those formats are built per-match, where the report's unit and the record's unit agree.

## System message

```
You segment cricket match reports for a research corpus. You select sentence indices. You
never write new text. You never invent facts. You output only compact JSON.
```

## User message

Rendered by `scripts/attribute.py`. The report is split into numbered sentences; the innings
roster is listed with batting side, bowling side and overs faced.

```
Assign each sentence to the innings it narrates.

Rules:
- Return sentence INDICES only, never text.
- A sentence describing a team's batting (their scoring, partnerships, collapse, chase)
  belongs to the innings that team BATTED in.
- A sentence describing bowling figures belongs to the innings the bowler was bowling IN —
  that is the innings where the opposing team batted.
- Sentences about the whole match, the toss, the result, conditions, the series, or a
  player's career go in "shared".
- Every index must appear exactly once across all buckets. Do not omit any.
- Order the indices inside each bucket by importance: most substantive first.
```

Output shape:

```json
{"innings": {"1": [ints], "2": [ints]}, "shared": [ints]}
```

## Repair, and what is counted as a failure

The builder never trusts the model's bookkeeping. After parsing it coerces indices to ints,
drops out-of-range values, removes duplicates (first bucket wins, in roster order), and
sweeps unassigned indices into `shared`. Every repair is counted in
`output/attribution_report.json`. A match is dropped only when the response is unparseable
after retries, or when an innings ends up with fewer than `min_sentences` of its own — that
innings is simply not narrated, and no record is emitted for it.
