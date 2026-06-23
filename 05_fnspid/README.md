# FNSPID → CPT World-Knowledge JSONL

> **Status: In progress** — demo CPT output from a 2-ticker local slice only. Full HuggingFace pipeline, text–ticker alignment, and dedup still TODO.

Daily stock close prices paired with same-day financial news articles for continued pre-training (CPT) on finance world knowledge.

---

## What this dataset is

[FNSPID](https://huggingface.co/datasets/Zihan1004/FNSPID) (Financial News and Stock Price Integration Dataset) is a large-scale finance corpus pairing **daily stock closing prices** with **same-day financial news articles** for **4,775 S&P500 companies**, covering **1999–2023**. The full HuggingFace release contains **29.7M stock price rows** and **15.7M news records** ([paper: arXiv 2402.06698](https://arxiv.org/abs/2402.06698)).

This package converts the source Alpaca SFT format (instruction/input/output) into CPT format: natural news prose + historical price series. Future prices from the Alpaca `output` field are **discarded** — only the history window enters the time series.

**Local demo files** (`FNSPID/FNSPID_train.json` + `FNSPID/FNSPID_val.json`) are a tiny slice: **2 tickers (AAL, A)**, **2019 only**, **400 records total**. The script reads these pre-downloaded files — it does not re-download from HuggingFace.

---

## ⚠️ Things to discuss before scaling this up

These are the issues to raise with Charon:

**1. News articles often cover the full market, not just the tagged ticker.**
Many articles are broad market commentary (e.g. "pre-market futures down") that gets attributed to multiple tickers on the same day. The text–ticker link is noisy — the news describes market context, not necessarily that specific company.

**2. The local demo files are a tiny slice.**
Only **AAL** (American Airlines) and **A** (Agilent Technologies), **2019 only**. The full scale requires downloading the HuggingFace dataset separately (see [Full dataset download](#full-dataset-download) below).

**3. No lookahead used.**
The future prices from the original Alpaca `output` field are intentionally discarded. Only historical prices (the history window defined in each source record) go into the time series.

**4. Sampling needed at scale.**
At **15.7M news records**, the full dataset needs dedup and sampling before CPT training — many articles are near-duplicates across tickers and dates.

**5. License.**
Dataset is for research use; commercial use restrictions apply per original FNSPID terms.

---

## What we are training for

**Objective:** Continued pre-training on world knowledge, not instruction fine-tuning. The model reads natural financial news text with a `<ts></ts>` placeholder; one daily close-price series is stored separately. There is no forecast prompt and no future prices in the output.

**What the model learns:** Finance world knowledge — that certain market language ("pre-market futures down," "5-day winning streak," "Fed patience on rates") co-occurs with specific price patterns. This is **not** a forecasting task.

**Example — single news article:**

```json
{
  "text": "AAL, 2019-01-31. Feb 1 () - Shares of Indian miner Vedanta Ltd slumped about 20 percent on Friday... Historical daily closing prices (USD) for AAL: <ts></ts>.",
  "timeseries": [
    {"values": [32.74, 31.65, 33.66, 34.98, 36.57, 36.29, 36.34], "unit": "close_price_usd", "freq": "daily"}
  ],
  "ticker": "AAL",
  "news_date": "2019-01-31",
  "history_start": "2019-01-22",
  "history_days": 7,
  "news_count": 1,
  "dataset": "fnspid",
  "source": "Zihan1004/FNSPID",
  "series_id": "AAL_2019-01-31",
  "task_type": "world_knowledge",
  "text_source": "financial_news_wire",
  "text_quality": "real"
}
```

**Example — multiple news articles concatenated:**

```json
{
  "text": "AAL, 2019-01-11. Friday, January 11, 2019\nPre-market futures this morning are down again...\nIt's quite the run, frankly, considering the lack of major economic data...\nThe bull-run for the U.S. stocks continued for the fifth straight day...\nMacy's Inc. M shares plunged 17.7%...\nInvestors in American Airlines Group Inc.AAL need to pay close attention... Historical daily closing prices (USD) for AAL: <ts></ts>.",
  "timeseries": [
    {"values": [32.48, 30.06, 32.04, 32.95, 32.42, 33.42, 32.04], "unit": "close_price_usd", "freq": "daily"}
  ],
  "ticker": "AAL",
  "news_date": "2019-01-11",
  "history_start": "2019-01-02",
  "history_days": 7,
  "news_count": 5,
  "dataset": "fnspid",
  "source": "Zihan1004/FNSPID",
  "series_id": "AAL_2019-01-11",
  "task_type": "world_knowledge",
  "text_source": "financial_news_wire",
  "text_quality": "real"
}
```

---

## Window design

**One record per (ticker, date) pair where news exists.**

- **History window** = all available price points before the news date in that Alpaca record (varies **5–30 days** in the full dataset; fixed at **7 days** in the local demo slice)
- **News date** = the Alpaca prediction date (the day the news articles were published)
- **Date range covered by prices:** `history_start` through the day before `news_date`
- No sliding window logic — the source Alpaca records already define the history windows

Default demo run: `max_records: 50` from the local JSON files.

---

## The one time series

| Field | Value | Description |
|:---:|---|---|
| 1 | `close_price_usd` | Daily stock closing price in USD |

Variable length per record (determined by the source Alpaca history window). Frequency label: `daily`.

---

## Quick start

```bash
cd datasets/05_fnspid
pip install -r requirements.txt

# Demo export: 50 records from local FNSPID JSON files
python scripts/build_cpt_jsonl.py --config config.example.yaml

# Dry-run: print one example record, no files written
python scripts/build_cpt_jsonl.py --dry-run

# AAL ticker only
python scripts/build_cpt_jsonl.py \
  --set data.tickers_filter=[AAL] \
  --set output.max_records=20

# Train split only
python scripts/build_cpt_jsonl.py --set data.split=train
```

Outputs:

- `output/fnspid_cpt.jsonl` — CPT records
- `output/run_report.json` — counts, skip reasons, ticker/date stats, config snapshot

---

## Full dataset download

The local demo files are **not** the full FNSPID release. For large-scale CPT, download from HuggingFace:

- Dataset page: https://huggingface.co/datasets/Zihan1004/FNSPID
- Paper: https://arxiv.org/abs/2402.06698

**News data (CSV):**

```bash
wget https://huggingface.co/datasets/Zihan1004/FNSPID/resolve/main/Stock_news/nasdaq_exteral_data.csv
```

**Stock price data:** Download the stock price archive from the HuggingFace dataset page (zip under `Stock_price/`). Update `config.example.yaml` paths to point at your converted Alpaca JSON or extend the script to read the raw CSV/parquet directly.

> Do not run these downloads as part of the demo build — the script uses the pre-downloaded local JSON files at `../../FNSPID/`.

---

## Configuration guide

| Section | Key | Purpose |
|---------|-----|---------|
| **data** | `train_path` | Path to `FNSPID_train.json` |
| | `val_path` | Path to `FNSPID_val.json` |
| | `split` | `both`, `train`, or `val` |
| | `tickers_filter` | Restrict to tickers; `[]` = all |
| | `min_history_days` | Skip records with fewer price points (default 5) |
| | `min_news_chars` | Skip records with insufficient news text (default 50) |
| **text** | `max_chars` | Truncate concatenated news to this limit (default 2000) |
| **output** | `output_path` / `report_path` | Output files |
| | `max_records` | Cap total records; `null` = no cap |
| | `indent` | `null` = compact JSONL |

---

## Files in this folder

| File | Purpose |
|------|---------|
| `README.md` | This document |
| `assessment.json` | Triage / probe metadata |
| `config.example.yaml` | Documented default configuration |
| `requirements.txt` | Python dependencies |
| `scripts/build_cpt_jsonl.py` | Alpaca JSON → CPT JSONL builder |
| `samples/example_output.jsonl` | Example single-article + multi-article records |
| `output/` | Generated JSONL + run reports |
