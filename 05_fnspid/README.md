# FNSPID → CPT

> **Status: Built** — full-scale HuggingFace pipeline. Latest build: **5,000 records**
> (seeded sample) across **2,849 tickers**, 2010–2023, from 15.5M scanned news rows.
> An optional relevance filter then keeps the **2,723** records whose article is genuinely
> about the paired ticker. Raise `output.max_records` to scale.
>
> **Committed here = trimmed 75-record viewable samples** (so they render on GitHub).
> The full build lives on the server — see [Output](#output).

**What it is:** Daily stock **OHLCV** prices + same-day financial news. One record =
**one** `(ticker, news_date)` where a real news article exists — the article text paired
with that ticker's trailing 30-trading-day price window (ending the day *before* the news,
so there's no lookahead).

**Scale:** Full HF news file ≈ **15.5M rows**, of which **~3.74M (24%)** carry a full
article body. After symbol/sampling filters → **~146k** valid `(ticker, date)` candidates;
the build emits a seeded random sample (default 5,000).

## The two output files

Both files share the **exact same schema**; the filtered one is a **strict subset** with a
relevance verdict attached. The difference is *which* `(ticker, date)` pairs are included:


| File                               | What's in it                                                                                                                                                                             | Full-build count | Use it for                                                           |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- | -------------------------------------------------------------------- |
| `output/fnspid_cpt.jsonl`          | **Raw pairing** — every `(ticker, date)` with a real article, after symbol/sampling filters. Still includes broad-market pieces that merely *mention* the ticker.                        | 5,000            | Maximum coverage; the unfiltered pairing.                            |
| `output/fnspid_cpt_filtered.jsonl` | **Relevance-filtered** — only records an LLM judge confirmed are genuinely *about* the ticker. ~46% dropped as market-wide noise (sector round-ups, articles about a different company). | 2,723            | **Higher-quality text↔ticker alignment — recommended for training.** |


The filter is a **judge, not a rewriter**: the article text is left untouched (stays
`text_quality:"real"`) and the judge sees only the article + ticker, never the price (no
leakage). Each filtered record carries the verdict in a `relevance` field, e.g.:
`{"model": "gemma4-31b-it", "relevant": true, "confidence": 1.0, "reason": "Detailed analysis of AAL options and stock price."}`.
Empirically the judge kept **2,723 / 5,000 (54.5%)** at avg confidence 0.99 — i.e. ~46% of
even symbol-tagged records were attribution noise.

## Record shape (multivariate + explicit trading dates)

```json
{
  "text": "AAL, 2023-03-30. We believe that American Airlines stock (NASDAQ: AAL)... Daily open, high, low, close, adjusted-close prices (USD) and share volume for AAL over the 30 trading days ending 2023-03-29: <ts></ts>.",
  "timeseries": [
    {"values": [/* open  */], "unit": "open_price_usd",      "freq": "1d"},
    {"values": [/* high  */], "unit": "high_price_usd",      "freq": "1d"},
    {"values": [/* low   */], "unit": "low_price_usd",       "freq": "1d"},
    {"values": [/* close */], "unit": "close_price_usd",     "freq": "1d"},
    {"values": [/* adj   */], "unit": "adj_close_price_usd", "freq": "1d"},
    {"values": [/* vol   */], "unit": "volume_shares",       "freq": "1d"}
  ],
  "task_type": "world_knowledge", "text_quality": "real",
  "ticker": "AAL", "news_date": "2023-03-30", "history_days": 30,
  "trading_dates": ["2023-02-15", "...", "2023-03-29"],
  "channels": ["open","high","low","close","adj_close","volume"],
  "series_id": "fnspid_AAL_2023-03-30", "license": "CC BY-NC 4.0"
}
```



## Design decisions (resolved)

Full rationale for each is in `[CHALLENGES.md](CHALLENGES.md)`; in brief:

- **Multivariate** — 6 OHLCV channels (not close-only), configurable via `data.channels`.
- **Irregular series handled honestly** — real trading days only, **no imputation**; each
record carries a `trading_dates` array aligned 1:1 with the values, so weekend/holiday
gaps are explicit (cf. Time-IMM, arXiv:2506.10412).
- **One ticker per record** — avoids the cross-stock "ragged panel" alignment problem
entirely (the only gaps left are per-series calendar gaps, handled above).
- **No lookahead** — the price window ends the trading day *before* `news_date` (validated).
- **Attribution noise addressed** — the optional LLM relevance filter (the two-file split above).
- **Deliberate, logged sampling** — per-ticker / per-month caps + seeded down-sample; every
drop reason is recorded in `output/run_report.json`.



## Open questions for the team lead

These are intentionally **not** decided — to confirm before scaling (details in `CHALLENGES.md`):

- **Default the relevance filter on?** And at what confidence threshold? *(§B1)*
- **Window length:** 30 trading days, or longer (60/120)? *(§B2)*
- `close` **vs** `adj_close`**:** keep both channels, or drop one near-duplicate? *(§A2)*
- **Cross-ticker duplicates:** collapse the same market-wide article across tickers, or keep one per ticker? *(§B4)*
- **Target corpus size & universe:** full ticker universe vs S&P500 only; date range; caps as set? *(§B5)*
- **Irregular-cadence contract (repo-wide):** keep `freq:"1d"` + `trading_dates`, or adopt a `"1b"` token / standard `timestamps` field? *(§A1)*
- **Window edge:** strictly history up to yesterday, or include the news-day close? *(§B3)*



## Source data (HuggingFace `Zihan1004/FNSPID`)


| File                                 | Size    | Use                                                         |
| ------------------------------------ | ------- | ----------------------------------------------------------- |
| `Stock_news/nasdaq_exteral_data.csv` | 23.2 GB | **Primary** — full wire, 24% with article bodies            |
| `Stock_news/All_external.csv`        | 5.73 GB | Fallback — only 9.6% have article bodies (mostly headlines) |
| `Stock_price/full_history.zip`       | 590 MB  | Per-ticker daily OHLCV (7,693 CSVs)                         |


Raw files download **outside the git repo** to `raw_data/05_fnspid/` (sibling of the repo),
so the multi-GB files are never committed.

## Run

```bash
pip install -r requirements.txt

# 1. download raw data (~once) -> ../../raw_data/05_fnspid/  (outside git)
python scripts/download_fnspid.py

# 2. smoke test (fast: limits rows + tickers)
python scripts/build_cpt_from_hf.py \
  --set data.tickers_filter=[AAL,AAPL] --set output.max_records=20 --set data.max_news_rows=3000000

# 3. capped build (full scan, ~5 min) -> output/fnspid_cpt.jsonl  (5,000 records)
python scripts/build_cpt_from_hf.py

# 4. full sampled build (all candidates at current caps)
python scripts/build_cpt_from_hf.py --set output.max_records=null

# 5. (optional) relevance filter -> output/fnspid_cpt_filtered.jsonl
#    needs an OpenAI-compatible local server (vLLM/SGLang/Ollama); quick test first:
python scripts/filter_news_relevance.py --limit 20 --verbose \
  --set relevance.base_url=http://localhost:8000/v1 --set relevance.model=<served-model>
python scripts/filter_news_relevance.py        # full pass
```



## Output

The `output/*.jsonl` committed in this repo are trimmed **75-record viewable samples** so they
render on GitHub. The **full build** (5,000 raw / 2,723 filtered — see `output/run_report.json`
and `output/relevance_report.json`) lives on the server at
`/data/defu/Time-series-datasets/05_fnspid/output/` and is regenerated by `build_cpt_from_hf.py`
(raise `output.max_records`). `samples/example_output.jsonl` is gitignored.

**Source:** [Zihan1004/FNSPID](https://huggingface.co/datasets/Zihan1004/FNSPID) ·
**License:** CC BY-NC 4.0 (research use only).