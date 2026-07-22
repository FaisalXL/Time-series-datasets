# FNSPID → CPT

> **Status: Built** — full-scale HuggingFace pipeline. Latest build: **5,000 records**
> (capped sample) across **2,849 tickers**, 2010–2023, from 15.5M scanned news rows
> (145,836 valid candidates before down-sampling). Raise `output.max_records` to scale.
>
> **Optional relevance filter run:** Gemma4-31b judge kept **2,723 / 5,000 (54.5%)**,
> dropping ~46% as broad-market noise → `output/fnspid_cpt_filtered.jsonl` (avg
> judge confidence 0.99). Real text preserved; per-record decision in `relevance`.

**What it is:** Daily stock **OHLCV** prices + same-day financial news. One record =
**one `(ticker, news_date)`** where a real news article exists.

**Scale:** Full HF news file (`nasdaq_exteral_data.csv`) ≈ **15.5M rows**, of which
**~3.74M (24%) carry a full article body**. After symbol/sampling filters → **145,836**
valid `(ticker, date)` candidates at the default caps; the build emits a seeded random
sample of those (default 5,000).

**Record shape (multivariate + explicit trading dates):**
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

**Key design decisions** (full write-up in [`CHALLENGES.md`](CHALLENGES.md)):
- **Multivariate** — 6 OHLCV channels, not close-only (configurable via `data.channels`).
- **Irregular series handled honestly** — market-closed gaps are kept as real trading
  days (no imputation); each record carries a `trading_dates` array aligned with the
  values so the calendar gaps are explicit (cf. Time-IMM, arXiv:2506.10412).
- **One ticker per record** — sidesteps the cross-stock "some stocks aren't trading at
  time *t*" panel-alignment problem (see `CHALLENGES.md` §A3).
- **No lookahead** — the price window ends the trading day *before* `news_date`.
- **Deliberate, logged sampling** — per-ticker / per-month caps + seeded down-sample;
  every drop reason is recorded in `output/run_report.json`.

## Source data (HuggingFace `Zihan1004/FNSPID`)

| File | Size | Use |
|------|------|-----|
| `Stock_news/nasdaq_exteral_data.csv` | 23.2 GB | **Primary** — full wire, 24% with article bodies |
| `Stock_news/All_external.csv` | 5.73 GB | Fallback — only 9.6% have article bodies (mostly headlines) |
| `Stock_price/full_history.zip` | 590 MB | Per-ticker daily OHLCV (7,693 CSVs) |

Raw files are downloaded **outside the git repo** to `raw_data/05_fnspid/` (sibling of
the repo) so the multi-GB files are never committed.

## Run

```bash
pip install -r requirements.txt            # (or use the conda env)

# 1. download raw data (~once) -> ../../raw_data/05_fnspid/  (outside git)
python scripts/download_fnspid.py

# 2. smoke test (fast: limits rows + tickers)
python scripts/build_cpt_from_hf.py \
  --set data.tickers_filter=[AAL,AAPL] --set output.max_records=20 \
  --set data.max_news_rows=3000000

# 3. capped build (full scan, ~5 min) -> output/fnspid_cpt.jsonl  (5,000 records)
python scripts/build_cpt_from_hf.py

# 4. full sampled build (all candidates at current caps)
python scripts/build_cpt_from_hf.py --set output.max_records=null

# 5. (optional) LLM relevance filter — drops broad-market articles, keeps real text.
#    Needs an OpenAI-compatible local server (vLLM/SGLang/Ollama). Quick test first:
python scripts/filter_news_relevance.py --limit 20 --verbose \
  --set relevance.base_url=http://localhost:8000/v1 --set relevance.model=<served-model>
python scripts/filter_news_relevance.py        # full pass -> output/fnspid_cpt_filtered.jsonl
```

**Optional relevance filter:** `scripts/filter_news_relevance.py` uses a local model as a
*judge* (not a rewriter) to drop articles that are broad-market commentary rather than
about the paired ticker — fixing the attribution noise (see `CHALLENGES.md` §B1) while
keeping the text fully real. It is shown only the article + ticker (never the price), so
there is no lookahead. Configure under `relevance:` in the config.

**Output:** `output/fnspid_cpt.jsonl` (default build ≈ 25 MB) + `output/run_report.json`.
A 50-record sample is written to `samples/example_output.jsonl` (gitignored).
For larger builds, keep the multi-GB JSONL out of git.

**Source:** [Zihan1004/FNSPID](https://huggingface.co/datasets/Zihan1004/FNSPID) ·
**License:** CC BY-NC 4.0 (research use only).
