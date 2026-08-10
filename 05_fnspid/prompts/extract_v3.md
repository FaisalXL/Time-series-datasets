# FNSPID extraction + summary prompt, v3 (window records)

One call returns **both** a coverage-constrained ranked extraction **and** a summary, so the
extractive-vs-summarised decision moves out of the GPU stage. Stage 3 picks which becomes
`text`; flipping costs one CPU pass and zero GPU.

## Why both, rather than choosing

The choice is not ours to make unilaterally — a summarised record must declare
`text_quality: generated`, and SCHEMA §7 permits `generated` only as "a small, tagged
minority" with sign-off. Emitting both means the schema conversation can happen against
measured records instead of a hypothesis, and either answer is a config flag rather than a
re-run.

## What v2 got wrong, measured

v2 asked for sentences ranked "most important first". That concentrates: **59% of kept
sentences came from a single article**, and only **2 of a median 7 available articles** were
represented. It was not a budget problem — utilisation was **46%** of the 2,240-char budget.
Ranking by importance simply piles onto the biggest story and never reaches the others.

v3 fixes the extraction with an explicit coverage constraint (at most two sentences per
article before moving on) and adds the summary for the cases where prose compresses better
than quotation.

Measured on 150 windows per length:

| | articles/record | text | numeric fidelity |
|---|--:|--:|--:|
| v2 extraction, 30d | 2 | 1,036 ch | n/a (verbatim) |
| v3 summary, 30d | **6** | 925 ch | **99.74%** |
| v2 extraction, 90d | 3 | 1,119 ch | n/a (verbatim) |
| v3 summary, 90d | **12** | 1,011 ch | **99.05%** |

Three to four times the coverage in fewer characters.

## The numeric-fidelity gate

Summaries are never trusted, they are checked: every number in the summary must occur in the
articles the model was shown. A record failing the check silently falls back to its
extraction, so a fabricated figure can never reach the corpus.

**The gate's own premise had to be checked first.** Its first version flagged ~50% of
summaries, which read as a damning hallucination rate and was almost entirely the checker's
fault — it left the trailing period on `2015.` so it never matched `2015`, and it compared
only against article bodies while the prompt *also* showed the model each article's
publication date. "August 23, 2015" was being scored as an invention. Corrected, the real
rate is 0.3–1.0%.

## System message

```
You classify financial news for a research corpus. You select and rank sentences, and you
write strictly factual summaries grounded only in the text you are given. You never predict
price movement. You output only compact JSON.
```

## Still unchanged from v2

**The model never sees the price series.** Showing it the window's returns and asking which
sentences explain them would improve every alignment metric we have, which is exactly why it
is not done: selecting or writing text to match the series manufactures the alignment and
voids the permutation control, the only measurement separating real correspondence from
coincidence.

## Builder-side rules after the call

| Check | Action |
|---|---|
| `role` is `incidental` or `absent` | Drop the record. |
| Summary contains a number not in the source | Fall back to the extraction. Never ship it. |
| Summary shorter than the floor | Fall back to the extraction. |
| Summary used | `text_quality: generated`, `text_source: generated`, all window articles credited. |
| Extraction used | `text_quality: real`, `text_source: third_party`. |
| Transport error, 500, unparsable JSON | Retry. Never count it as a verdict. |

## Open

The budget is still only ~41% used (925 of 2,240 chars) because the prompt caps the summary
at 220 words. Raising that toward ~350 words would fill the budget and buy more coverage
again; it needs one more measurement pass before being set.
