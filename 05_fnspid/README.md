# FNSPID → CPT

> **Status: In progress** — demo uses 2 tickers / 2019 only. Full HuggingFace pipeline not built yet.

**What it is:** Daily stock close prices + same-day financial news. One record = **one (ticker, date)** where news exists.

**Scale:** Full HF release ≈ **15.7M news rows** → realistically **~2–4M CPT records** after dedup and days-without-news drops. Demo: **50 records** from 400-row local slice (AAL, A, 2019).

**Record shape:**
```json
{
  "text": "AAL, 2019-01-11. Pre-market futures are down again... markets riding a 5-day winning streak... Historical daily closing prices (USD) for AAL: <ts></ts>.",
  "timeseries": [
    {"values": [32.48, 30.06, 32.04, 32.95, 32.42, 33.42, 32.04], "unit": "close_price_usd", "freq": "daily"}
  ],
  "ticker": "AAL", "news_date": "2019-01-11", "history_days": 7, "task_type": "world_knowledge"
}
```

**Key issues:**
- **Text–ticker link is noisy** — articles are often broad market commentary (Fed, sector moves) tagged to many tickers.
- **Temporal misalignment for CPT** — news is on day D, prices are the 7 days *before* D (forecasting layout). Text describes "today"; series ends yesterday.
- **~40% of trading days have no news** for quieter tickers (38% coverage for Agilent vs 82% for AAL in our EDA).
- **CC BY-NC 4.0** — research use only.

**Run:** `pip install -r requirements.txt && python scripts/build_cpt_jsonl.py` (requires local Alpaca JSON at `../../FNSPID/` or HF rebuild)

**Output:** `output/fnspid_cpt.jsonl` · **Source:** [Zihan1004/FNSPID](https://huggingface.co/datasets/Zihan1004/FNSPID)
