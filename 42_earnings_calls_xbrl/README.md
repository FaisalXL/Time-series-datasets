# Earnings-Call Transcripts + SEC XBRL Fundamentals → CPT

> ## Status: FULL BUILD (2026-08-20) — **23,202 records**, `23,202/23,202 --strict`, 0 warnings.
>
> **HELD, not shippable.** Every record is tagged `proprietary-review`. The HuggingFace dataset card
> says MIT, but an earnings-call transcript is produced by a transcription provider and an uploader
> cannot license what it does not hold; the frozen v1 enum has no MIT slot, and SCHEMA §6 defines
> `proprietary-review` as "excluded from any release until cleared". Built on an explicit
> instruction to proceed regardless of the licence question — `data.license` in the config is a
> one-line flip. The full build lives in `cpt_corpus/held/42_earnings_calls_xbrl/`.
>
> **The previous "~25k+" counted the transcript side and assumed the join was free.** It was not:
> the builder as written yielded **9,372**. See "Where the records come from".

| | measured |
|---|---|
| Records | **23,202** from 26,361 transcripts (88.0%), 590 tickers, 2012–2025 |
| Channels | mean **2.88** (3 channels: 20,372 · 2 channels: 2,830) |
| Depth | 12 quarters, every record; **2.54%** of value slots null |
| Alignment | `describes` **20,032** · `recites` **3,170** (13.7%, at 42x over a permutation control) |
| Text | mean 7,429 chars, whole speaker turns, verbatim + one bare `<ts></ts>` |
| Duplicates | **0** duplicate texts, **0** duplicate `series_id` |
| Series currency | 88.5% of records' series include the quarter the call is about |
| Vintage splice | 13,555 records use a spliced revenue concept; 20,003 record a **rejected** one |

**What it is:** One record = **one (company, fiscal quarter)** — the earnings-call transcript (where the exec recites the quarter's revenue / net income / EPS) paired with that company's **trailing 12-quarter fundamentals** from SEC EDGAR XBRL. The narration *describes* the numbers → the tightest text↔series alignment of any candidate.

**Scale:** 33,362 transcripts (685 companies, 2005–2025) on the text side; SEC XBRL fundamentals cover thousands of filers 2009→present. After the 2012-start + ≥12-quarter-window filters, **~25k+ joinable records** (full SEC universe would be 100k+).

#### 📄 Text — earnings-call transcript
| | |
|---|---|
| **What** | Full verbatim earnings-call transcript (operator intro → management prepared remarks → analyst Q&A). The exec recites the quarter's figures. |
| **Source** | HuggingFace `Bose345/sp500_earnings_transcripts` (**MIT**), single parquet — read with **duckdb** (no pandas/pyarrow). Fields: `symbol`, `quarter`, `year`, `date`, `content`. |
| **Format** | ~40k chars/transcript; we keep the leading `text.max_text_chars` (default 12,000 — the prepared-remarks portion, where the numbers are recited). |
| **`text_quality`** | `"real"` |

#### 📈 Time series — SEC XBRL quarterly fundamentals
| | |
|---|---|
| **What** | 3 channels of quarterly fundamentals, trailing 12 quarters |
| **Source** | SEC EDGAR XBRL `companyfacts` API — `data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json` (free, **requires a descriptive `User-Agent`**). Ticker→CIK via `sec.gov/files/company_tickers.json`. |
| **Cadence** | `1q`, 12-quarter trailing window ending at the reported quarter |

| Channel (`unit`) | XBRL concept |
|---|---|
| `revenue_usd` | `RevenueFromContractWithCustomerExcludingAssessedTax` (fallback `Revenues`) |
| `net_income_usd` | `NetIncomeLoss` |
| `eps_diluted_usd_per_share` | `EarningsPerShareDiluted` |

Extraction: quarterly-**duration** facts (80–100-day period), keyed by period-end, aligned on common ends ≤ the call date → a clean contiguous quarterly series (avoids the gappy CY-frame approach).

**Record shape:** (real — Deere & Company, Q2 2025; revenue/net-income arrays in USD, abbreviated).
The `<ts></ts>` tag is appended directly to the real transcript excerpt — **no framing/bridging
sentence is generated**; every word before the tag is verbatim call transcript, truncated at
`text.max_text_chars`:
```json
{
  "text": "Operator: Good morning, and welcome to Deere & Company's Second Quarter Earnings Conference Call...\n\n...Operating profit was down year-over-year at $379 million, r\n\n<ts></ts>",
  "timeseries": [
    {"values": [11500000000, 9600000000, "...", 12800000000], "unit": "revenue_usd", "freq": "1q"},
    {"values": [1700000000, 900000000, "...", 1800000000], "unit": "net_income_usd", "freq": "1q"},
    {"values": [5.32, 2.92, 6.81, 6.16, 6.55, 9.65, 10.2, 6.23, 8.53, 6.29, 3.19, 6.64], "unit": "eps_diluted_usd_per_share", "freq": "1q"}
  ],
  "task_type": "world_knowledge", "text_quality": "real",
  "ticker": "DE", "cik": "0000315189", "company_name": "Deere & Company",
  "fiscal_quarter": "Q2 2025", "reported_quarter_end": "2025-04-27", "call_date": "2025-05-15", "window_quarters": 12,
  "dataset": "earnings_calls_xbrl", "source": "huggingface.co/Bose345 + data.sec.gov"
}
```

**Key issues:**
- **No generated text.** An earlier version of this build appended a templated closing sentence to introduce the revenue/net-income/EPS series before `<ts></ts>`. That sentence was not from the earnings call transcript — it was synthesized by the build script. Fixed 2026-07-24: `<ts></ts>` is now appended directly to the real transcript excerpt with nothing generated in between.
- **⚠️ Overlap with the team's SEC EDGAR dataset — confirm before scaling.** The XBRL *fundamentals* side may duplicate that work; the *novel* element is the transcript↔fundamentals pairing. Get a quick "not redundant" sign-off from Charon before the full build.
- **Alignment = describes (strongest we have)** — the exec states revenue/EPS = the XBRL numbers. Leakage of the reported quarter's value into the text is inherent and *the point*.
- **Q4 / annual gap:** companies file Q4 inside the 10-K as a full-year figure, so there's **no standalone Q4 quarterly fact** — Q4-call windows end at Q3 (Q1–Q3 calls align exactly to the reported quarter). Optional fix: compute Q4 = annual − 9-month sum.
- **Environment:** reads the parquet with **duckdb** (no pandas/pyarrow), so it works even on Python 3.14; SEC access uses stdlib `urllib`.
- **SEC etiquette:** descriptive `User-Agent` required; `companyfacts` cached per CIK; ~0.15 s delay (SEC allows ~10 req/s).
- **Demo output ≈ 639 KB** for 50 long transcripts; lower `output.max_records` or `text.max_text_chars` for a more GitHub-friendly sample.

## Where the records come from, and what the old join lost

| stage | records |
|---|--:|
| transcripts in the parquet | 33,362 |
| after the `min_transcript_date: 2012-01-01` filter | 26,361 |
| **what the previous builder's join yielded** | **9,372** |
| after fixing the revenue concept (below) | 22,593 |
| **+ de-listed ticker recovery → shipped** | **23,202** |

Skip reasons, kept separate because they are different problems:
946 `short_window` (<12 quarters of history) · 814 `no_cik` · 712 `fewer_than_2_channels` ·
528 `too_many_nulls` (>25%) · 84 `no_xbrl_facts` · 72 `dup_text` · 3 `short_text`.
Sums to 3,159, and 26,361 − 3,159 = 23,202 exactly.

### ASC 606 renamed revenue in 2018, and "first concept wins" threw away everything before it

`RevenueFromContractWithCustomerExcludingAssessedTax` arrived with ASC 606; before it, filers used
`SalesRevenueNet`. The old builder tried concepts in order and kept the first that returned
anything. For Abbott that meant keeping ASC 606's 31 quarters (2017→2026) and **discarding
`SalesRevenueNet`'s 40 quarters (2008→2017)**. Measured across 575 filers: **442 were losing
quarters this way, a mean of 32 each** — which is why the dominant skip was "revenue channel
empty", 85% of the loss, concentrated in 2012–2017.

### The obvious fix is wrong, so the splice is per-filer and overlap-verified

Merging every revenue concept puts **71.1% of shared period-ends in disagreement** (median gap
36%). The largest disagreeing pair is `SalesRevenueGoodsNet` vs `SalesRevenueServicesNet` — those
are **components** of revenue, not alternative spellings, and splicing a component onto a total
gives a series that silently switches between total revenue and goods-only revenue. Even restricted
to total-level concepts, agreement is only **51.3%**, because `Revenues` often includes interest or
other income that ASC 606 revenue excludes (`Revenues` vs `RevenuesNetOfInterestExpense`: **9%**).

So [`scripts/index_series.py`](scripts/index_series.py) anchors on the concept reaching the latest
quarter and extends backwards **only** with a concept that matches the anchor on *every* shared
period-end to 0.5%. 277 filers splice (+7,082 quarters, median +28); **204 filers have a concept
rejected**. `meta.xbrl_concepts` and `meta.xbrl_concepts_rejected` record both per record, so a
spliced series is auditable rather than asserted.

## ⚠️ `--strict` passing does not make the `recites` tag honest

The shared gate accepts any text number within **1% of any value in any channel**. On a
12,000-char earnings-call excerpt that is nearly vacuous:

| test | real | permutation control | lift |
|---|--:|--:|--:|
| the gate's own test (any channel, any quarter, 1%) | 62.5% | 59.7% | **1.05x** |
| exact, signed, **terminal quarter only** | 13.8% | 0.3% | **42.4x** |
| exact, signed, any of the 12 window quarters | 17.0% | 3.9% | 4.3x |

The control pairs each transcript with a **different company's** series, so every hit is
coincidence by construction. Tagging on the gate's test would have shipped **14,502 `recites`**
of which ~96% was noise — and 23,202/23,202 passed `--strict` with 0 warnings while it did.

This package tags on the 42x rule. Two supporting measurements: revenue appears as raw digits in
**6 of 19,454** records (0.0%) — execs say "$12.8 billion", and unit-scale reconciliation is not
reciting under SCHEMA §7 (the ruling already applied to `58_fas_gain_attache`) — and the 42x lift
holds in every EPS-magnitude stratum (15.9x for |eps|<1 up to 50x for |eps|≥10).

## Q4 has no quarterly fact, and deriving one is not safe

A Q4 call's figures are filed inside the 10-K as a full year, so no standalone Q4 quarterly fact
exists and a Q4 window ends at Q3. The obvious fix — Q4 = FY − 9M — was cross-checked against
filers that *do* have a real Q4 quarterly fact: it matches **82.7%** of the time for revenue and
**92.7%** for net income, with failures up to **168% wrong** (one derived revenue came out at
**−$7.07 bn**). Not shipped.

Instead every record carries `meta.series_end_lag_days` and `meta.reported_quarter_in_series`.
**88.5%** of records include the quarter the call discusses; the 2,529 that do not are almost all
Q4 calls, and they say so. The previous sample record silently labelled itself `"Q4 2025"` over a
series ending `2024-12-31`; that contradiction is now a field instead of a surprise.

## De-listed tickers: `company_tickers.json` only lists the living

SEC's ticker file covers **currently listed** filers, so every acquired, renamed or de-listed
company silently disappears — **74 of 651 symbols, 2,218 transcripts (8.4%)**. `BK` now trades as
`BNY` and `MMC` as `MRSH` (same CIK, still filing); EA, Juniper, Kellanova and Walgreens left the
file in 2025. Their XBRL is still on file under the old CIK, so this is recoverable loss, not
absent data. EDGAR's `browse-edgar?ticker=` resolves historical tickers and recovered **43 of 74**
(+1,404 transcripts) into `.cache/ticker_overrides.json`. The remaining 31 (BF.B, Fiserv, Cerner,
Xilinx, Tiffany, …) are gone from that index too; name-based lookup cannot help because
`company_name` is null for 58 of the 74 rows in the parquet.

## Four defects a 50-record demo was too small to expose

* **Memory did not scale.** The builder held every parsed `companyfacts` payload for the whole run:
  575 filers × 4.6 MB = **2.1 GB raw**, several times that once parsed. Fine at 50 records, an OOM
  at 26,361. [`scripts/index_series.py`](scripts/index_series.py) turns that 2.1 GB into a **2.7 MB**
  index, and the build reads only the index.
* **Text was cut mid-word.** `content[:12000]` on a 53,501-char mean transcript sliced sentences —
  the old sample ended `"...was down year-over-year at $379 million, r"`. `structured_content` is a
  list of `{speaker, text}` turns, so the budget is now spent in whole turns (23,200 of 23,202
  records; 2 fall back to the raw prefix).
* **TLS verification was disabled** — `check_hostname = False`, `verify_mode = CERT_NONE` for both
  SEC and HuggingFace. The default verified context returns 200 for both.
* **The committed sample was a pretty-printed JSON array** under a `.jsonl` name, so `json.loads`
  died on line 2 and any per-line consumer globbing `*.jsonl` broke on it. Fourth appearance of
  that defect in this corpus.

## Provenance fields were entirely absent

The demo emitted no `license`, `domain`, `region` or `text_source` at all — Part 1 counted 50
`missing_provenance` records here. Now: `license: proprietary-review`, `domain: finance`,
`region: US`, `text_source: third_party` (the exec's own words, but a third party's transcription),
and `source` is the canonical dataset URL rather than the free-text `"huggingface.co/Bose345 +
data.sec.gov"` the schema flagged as not-a-URL.

**Run:**
```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=5   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50)
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full build (~25k+)
```

**Output:** `output/earnings_calls_xbrl_cpt.jsonl` + `output/run_report.json` (`samples/` gitignored; `.cache/` holds the parquet + per-CIK companyfacts so reruns are free).

**Sources:** [Bose345/sp500_earnings_transcripts](https://huggingface.co/datasets/Bose345/sp500_earnings_transcripts) (MIT) · [SEC EDGAR XBRL](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) (public domain).
