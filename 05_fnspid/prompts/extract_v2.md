# FNSPID extraction + salience prompt, v2 (window records)

v1 judged one article against one ticker. v2 judges **a window's worth of coverage** — every
article published about the ticker during the W trading days the series covers — and returns
the sentences **ranked by importance**.

## What changed from v1, and why

1. **The model ranks; the builder budgets.** v1 returned an unordered set of indices and the
   builder then cut the assembled text at 2,240 chars, which discarded model-selected content
   in 46% of records — the same blind truncation extraction was meant to replace. v2 returns
   indices *ordered by importance*, so the builder fills the ~500-token budget by taking a
   prefix of the model's own ranking. Nothing chosen is arbitrarily dropped; what falls
   outside the budget is what the model itself ranked last.
2. **Sentences span several articles.** Numbering is global across the window's articles, and
   each article is introduced by its publication date, so the model can prefer the substantive
   report over the follow-up rehash.
3. **Still indices, never text.** The builder assembles verbatim, so `text_quality` stays
   `real`. This matters more than in v1: the frozen v1 vocab is
   `TEXT_QUALITY = {real, generated}` — there is no `llm_summarized` slot — and SCHEMA §7
   allows `generated` only as "a small, tagged minority" with sign-off. Extraction is the only
   route to a budgeted text that does not put the corpus's largest package into that bucket.

## What deliberately did NOT change

**The model never sees the price series.** It would be easy to show the window's returns and
ask for the sentences that explain them, and it would score better on every alignment metric
we have — which is exactly why it is not done. Selecting text to match the series manufactures
the alignment and destroys the permutation control, the one measurement that distinguishes
real text-series correspondence from coincidence. The model ranks on editorial salience alone;
whether the result aligns with the series is then something we can honestly measure.

The model is also still forbidden to predict direction. This is a corpus, not a forecasting set.

## System message

```
You classify financial news for a research corpus. You select and rank sentences. You never
write new text. You never predict price movement. You output only compact JSON.
```

## User message template

Placeholders: `{TICKER}`, `{COMPANY}`, `{PERIOD_START}`, `{PERIOD_END}`, `{NUMBERED_SENTENCES}`.

```
TICKER: {TICKER}
COMPANY: {COMPANY}
PERIOD: {PERIOD_START} to {PERIOD_END}

Below is every article published about this company during that period, as numbered
sentences. Each article is introduced by its publication date.

{NUMBERED_SENTENCES}

Do three things.

1. Decide the role of {COMPANY} across this period's coverage. Use exactly one value:
   "primary"    - at least one article reports on {COMPANY} itself: its results, guidance,
                  products, management, filings, analyst coverage of it, or its own share move.
   "secondary"  - no article is about {COMPANY}, but at least one states a specific effect on
                  it: a supplier or customer event, a named peer comparison, a rate or
                  commodity move the article ties to this company or its sector.
   "incidental" - {COMPANY} is named but never substantively discussed: ticker enumerations,
                  one-line table rows, passing comparisons, promotional boilerplate.
   "absent"     - the coverage does not discuss {COMPANY} at all.

2. Rank the sentence numbers that a reader would need to understand what happened to
   {COMPANY} during this period. MOST IMPORTANT FIRST. Only the first part of your list will
   be used, so the order carries the decision. Prefer sentences that report a concrete event,
   figure, or decision over ones that restate context. Do not include a sentence that says
   nothing about {COMPANY}. Do not invent numbers. Return an empty list for "absent".

3. Write the relation in 12 words or fewer. Name the mechanism. Do not restate a headline.

Output this JSON only:
{"role":"primary|secondary|incidental|absent","sentences":[int,...],"relation":"...","confidence":0.0-1.0}
```

## Builder-side rules after the call

| Check | Action |
|---|---|
| `role` is `incidental` or `absent` | Drop the record. |
| `sentences` is empty | Drop the record. |
| An index does not exist | Drop the index. Log it. Never fail open. |
| Selected text exceeds the budget | Take the model's ranking prefix that fits. Never tail-cut. |
| Assembled text is under the character floor | Drop the record. |
| Transport error, 500, or unparsable JSON | Retry. Never count it as a verdict. |

After the budget prefix is chosen, the kept sentences are re-sorted into **document order**
before assembly, so the record reads as prose rather than as a salience ranking.
