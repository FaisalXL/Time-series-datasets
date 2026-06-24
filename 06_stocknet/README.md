# StockNet → CPT

> **Status: Demo** — 50 sample records. Full build: ~7,000–9,000 records (88 stocks × ~100 ISO weeks, after filters).

**What it is:** Investor/trader tweets paired with daily OHLCV for the same calendar week. One record = **one (ticker, ISO-week) pair**. Window size: **1 week** (ISO week) — all unique tweets for the week form the text; the 5 daily OHLCV bars for the same week form the timeseries.

**Scale:** 88 S&P-selected stocks across 9 sectors. Tweet window: Jan 2014 – Jan 2016 (~104 ISO weeks per ticker). Unique tweet deduplication and minimum-tweet filter reduce this by ~15–25%.

**Record shape:**
```json
{
  "text": "Investor commentary on Apple Inc. (AAPL; Technology) for the week of January 6–10, 2014: \"Wall st. kicks off new year on lower note.\" \"Strong buy on $AAPL for 2014.\" Daily open, high, low, close prices (USD) and share volume for this week's trading sessions: <ts></ts>",
  "timeseries": [
    {"values": [76.45, 76.60, 77.09, 77.44, 77.16], "unit": "open_usd",      "freq": "1d"},
    {"values": [77.07, 77.20, 77.50, 77.93, 77.94], "unit": "high_usd",      "freq": "1d"},
    {"values": [75.98, 76.21, 76.79, 77.10, 76.86], "unit": "low_usd",       "freq": "1d"},
    {"values": [76.56, 76.85, 77.31, 77.73, 77.28], "unit": "close_usd",     "freq": "1d"},
    {"values": [65124400, 58992800, 53589200, 60108500, 76277900], "unit": "volume_shares", "freq": "1d"}
  ],
  "task_type": "world_knowledge",
  "text_quality": "real",
  "ticker": "AAPL", "sector": "Technology", "week_start": "2014-01-06", "n_tweets": 14
}
```

**Key issues:**
- **Text quality flag** — tweets are third-party social media, not official first-party text (Charon rule #5). Currently tagged `text_quality: "real"`. Confirm with Charon whether this is acceptable or if a separate tag is needed.
- **Retweet noise** — many tweets are identical retweets; script deduplicates within each week by normalised text. Residual near-duplicate paraphrases remain.
- **AT_USER / URL tokens** — StockNet's preprocessor replaced all @mentions with `AT_USER` and all hyperlinks with `URL`. These placeholder tokens appear verbatim in the emitted text.
- **Token-array format** — preprocessed files store tweet text as a JSON token array (lowercase); script rejoins with basic punctuation detokenisation.
- **Cross-ticker language** — tweets about one stock frequently mention others (e.g. `$AAPL vs $MSFT`); the timeseries is for the primary ticker only.
- **Holiday truncation** — weeks with < 3 trading days are skipped (configurable via `filters.min_trading_days`).
- **High tweet volume** — active tickers produce 30–330 unique tweets per week (AAPL averages ~130). The full text block can become very long. Cap with `filters.max_tweets_per_record: 20` or leave `null` to include all.
- **Sector labelling** — StockTable uses 2014-era sector classifications (e.g. AAPL → "Consumer Goods") which differ from current indices.

**Run:**
```bash
cd datasets/06_stocknet
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py                              # demo (50 records)
python scripts/build_cpt_jsonl.py --set output.max_records=null  # full build
```

**Output:** `output/stocknet_cpt.jsonl` · `output/run_report.json`

**Source:** [yumoxu/stocknet-dataset](https://github.com/yumoxu/stocknet-dataset) (MIT) · Xu & Cohen, ACL 2018
