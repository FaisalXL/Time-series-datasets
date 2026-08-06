# FNSPID extraction + salience prompt, v1

One LLM call does two jobs. It selects the ticker-relevant sentences. It rates salience.
This replaces a separate relevance judge. Extraction length and salience are the same signal.

## Design rules

1. **The model returns sentence INDICES, never text.** The builder assembles the text verbatim
   from the source. This keeps `text_quality: real`. It avoids the `llm_summarized` sign-off
   gate. It cuts output from ~400 tokens to ~20 tokens.
2. **The model never sees the price series.** No leakage is possible.
3. **The model must not predict price direction.** The corpus is not a forecasting dataset.
4. **The model must name the relation.** A named relation makes the verdict auditable.
5. **Temperature 0 and a pinned model.** The builder stores indices in `meta`. A rebuild then
   reproduces the corpus without new LLM calls.

## System message

```
You classify financial news for a research corpus. You select sentences. You never write new
text. You never predict price movement. You output only compact JSON.
```

## User message template

Placeholders: `{TICKER}`, `{COMPANY}`, `{NUMBERED_SENTENCES}`.

```
TICKER: {TICKER}
COMPANY: {COMPANY}

The article appears below as numbered sentences.

{NUMBERED_SENTENCES}

Do three things.

1. Decide the role of {TICKER} in this article. Use exactly one value:
   "primary"    - the article reports on {COMPANY} itself: its results, guidance, products,
                  management, filings, analyst coverage of it, or its own share move.
   "secondary"  - the article reports on something else, but states a specific effect on
                  {COMPANY}. Examples: a supplier or customer event, a named peer comparison,
                  a rate or commodity move that the article ties to this company or its sector,
                  index or ETF membership that the article discusses.
   "incidental" - the article names {COMPANY} but gives it no substantive discussion. It
                  appears in a ticker enumeration, a one-line table row, a passing comparison,
                  or promotional boilerplate. List membership alone does not decide this. If
                  the article devotes its own paragraph to {COMPANY}, choose "primary" or
                  "secondary" instead.
   "absent"     - the article does not name or discuss {COMPANY} at all.

2. List the sentence numbers that discuss {COMPANY}. Include a sentence only if it states
   something about {COMPANY}. Include a following sentence when it continues the same subject
   through a pronoun such as "the company" or "its". Return an empty list for "absent".
   Order the numbers as they appear. Do not invent numbers.

3. Write the relation in 12 words or fewer. Name the mechanism. Do not restate the headline.

Output this JSON only:
{"role":"primary|secondary|incidental|absent","sentences":[int,...],"relation":"...","confidence":0.0-1.0}
```

## Builder-side rules after the call

| Check | Action |
|---|---|
| `role` is `incidental` or `absent` | Drop the record. |
| `sentences` is empty | Drop the record. |
| An index does not exist | Drop the index. Log it. Never fail open. |
| Assembled text is under the character floor | Drop the record. See the floor note below. |
| Assembled text is over the token cap | Truncate at a sentence boundary under the cap. |
| Transport error, 500, or unparsable JSON | Retry. Never count it as a verdict. |

**Never keep a record on a parse error.** The previous config set `keep_on_parse_error: true`.
With a thinking model that returns `content: null`, that setting keeps 100% of records. The
filter then appears to run and does nothing.

## The floor is unresolved

Measured on the devset, deterministic sentence matching, lower bound:

| Group | Median extract | Under 300 chars |
|---|--:|--:|
| primary subject = record ticker | 354 chars | 10 of 27 |
| primary-subject mismatch | 200 chars | 14 of 17 |
| the AAL / Delta false positive | 105 chars | rejected |

A 300-character floor removes 82% of mismatches. It also removes 37% of correct records.
So the floor alone over-rejects. Use `role` for the keep decision. Use the floor only as a
second guard. Measure both rates on the 3,000-record sample before you fix the value.

## Open prompt questions for the owner

1. Does `secondary` enter the corpus, or only `primary`? `secondary` raises volume. It also
   raises the `contextualizes` share, which SCHEMA §7 already constrains.
2. Should the model keep the article's opening sentence always, for context, even when it does
   not name the company?
3. Does a per-article record cap apply? One macro article can produce many `secondary` records.
