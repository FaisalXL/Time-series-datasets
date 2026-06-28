# FNSPID — Self-Contained Server Build Brief

*For building the full-scale FNSPID CPT dataset on the SSH server. This file is
self-contained: it assumes you cloned only this git repo (root = `datasets/`) and
have **no** access to any parent workspace. Everything you need is below. Build the
pipeline **from scratch** off the raw HuggingFace data — there is no prior pipeline to
extend.*

---

## 0. TL;DR — two deliverables

**Deliverable A — the build script + sample output.** A fresh `scripts/build_cpt_from_hf.py`
that turns the raw FNSPID news + price files into CPT records (`output/fnspid_cpt.jsonl`),
following the exact same pattern as the other datasets in this repo (config-driven,
`run_report.json`, validated records).

**Deliverable B — a challenges write-up (`CHALLENGES.md`).** A short doc capturing the design
decisions and open questions this dataset raises (§6), so the repo owner can take them to the
team lead before scaling. This is as important as the script — surface the hard calls, don't
silently pick them.

Steps:
1. Clone the repo, make a venv, install deps.
2. Download `Zihan1004/FNSPID` from HuggingFace into `.cache/` (~6–30 GB — see §4).
3. Write `scripts/build_cpt_from_hf.py`, pairing news ↔ price history per `(ticker, date)` (§5).
4. Emit `output/fnspid_cpt.jsonl` in the CPT contract (§2). Validate (§8).
5. Write `CHALLENGES.md` (§6).
6. **Never commit** the downloaded GBs or a multi-GB output — `.cache/` is gitignored;
   keep large outputs out of git (§7).

> **This brief is the single source of truth for the FNSPID build** — you do not need any
> other doc in the repo. Ignore other top-level files; in particular do not rely on these
> paths, which exist only in the original local workspace and are **not in this clone**:
> `../../FNSPID/`, `../From_News_to_Forecast/`, `../PROJECT_CONTEXT.md`, `../defu_30_registry.csv`.
> Everything you need is inlined here.

---

## 1. What we are building

**Continued Pre-Training (CPT) world-knowledge data** — instruction-free records that pair
**real first-party text** with an **aligned time series**, so a multimodal model learns from
both modalities together.

For FNSPID: one record = **one `(ticker, news_date)`** — the real financial-news text
published on that date, paired with that ticker's **trailing daily close-price window**
(prices *up to and including the day before* the news date; no lookahead).

**Core alignment rule:** natural, source-native alignment — the text must genuinely describe
the same phenomenon the numbers represent. No arbitrary sliding-window inflation, no
duplicate-boilerplate scale padding.

---

## 2. Target record shape (the contract)

One JSON object per line in `output/fnspid_cpt.jsonl`:

```json
{
  "text": "AAL, 2019-01-11. Pre-market futures are down again ... Historical daily closing prices (USD) for AAL: <ts></ts>.",
  "timeseries": [
    {"values": [32.48, 30.06, 32.04, 32.95, 32.42, 33.42, 32.04], "unit": "close_price_usd", "freq": "1d"}
  ],
  "task_type": "world_knowledge",
  "text_quality": "real",
  "ticker": "AAL",
  "news_date": "2019-01-11",
  "history_days": 7
}
```

Required conventions:

| Field | Rule |
|-------|------|
| `text` | Natural news prose; **exactly one** `<ts></ts>` (in a short closing sentence). No task instructions. |
| `timeseries` | List of channels, each `{values, unit, freq}`. `freq` is **compact lowercase** (`1d`). |
| `task_type` | Always `"world_knowledge"`. |
| `text_quality` | `"real"`. |
| Extras | `ticker`, `news_date`, `history_days`, `series_id`, etc. allowed after the required fields. |

**Window decision (matches our other datasets):** a trailing window of close prices ending
the trading day **before** `news_date` — the text describes "today"; the series is the run-up
to today, so there is no lookahead. Length is a config knob (`history_days`, e.g. 7–30). Drop
a record if fewer than `min_history_days` prices are available.

---

## 3. What's already in this folder

- `README.md` — status + record shape + caveats.
- `assessment.json` — source URLs and the pairing method/caveats. Use it for the HF URL and
  the caveat list (§6).
- `config.example.yaml` — has the config knobs (`history_days`, `min_history_days`,
  `min_news_chars`, `text.max_chars`, `output.max_records`). Reuse the knobs; repoint `data`
  at the HF files.
- `scripts/build_cpt_jsonl.py` — a small **demo** script for a tiny local slice that is **not
  in this clone**. It is not the basis for this build. You may borrow only its boilerplate
  (the YAML config loader supporting `--config` and `--set dotted.key=value`); build the data
  pipeline fresh from the raw HF files.
- `requirements.txt` — currently just `pyyaml`. Add the deps in §4.

---

## 4. Source data + download (HuggingFace `Zihan1004/FNSPID`)

Repo total ≈ **29.6 GB**. Actual files (verified):

| Path | Size | What |
|------|------|------|
| `Stock_news/nasdaq_exteral_data.csv` | **23.2 GB** | Full news wire — every article (largest). |
| `Stock_news/All_external.csv` | **5.73 GB** | Smaller curated news set — **start here**. |
| `Stock_price/full_history.zip` | **590 MB** | Per-ticker daily OHLCV CSVs (`full_history/<TICKER>.csv`). |

**Recommendation:** start with `All_external.csv` (5.73 GB) + `full_history.zip` (590 MB)
≈ **6.3 GB** for a first full build, then scale to `nasdaq_exteral_data.csv` if needed.
Check free disk first (`df -h .`): you need room for the raw files **plus** the extracted
price CSVs **plus** the output.

Deps:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install huggingface_hub pandas pyarrow      # add these to requirements.txt
```

Download just the files you want (not the whole 29.6 GB) into `.cache/`:
```python
# scripts/download_fnspid.py  (or run inline)
from huggingface_hub import hf_hub_download
REPO = "Zihan1004/FNSPID"
for f in ["Stock_news/All_external.csv", "Stock_price/full_history.zip"]:
    p = hf_hub_download(repo_id=REPO, filename=f, repo_type="dataset",
                        local_dir=".cache/raw")
    print(p)
```
Then `unzip .cache/raw/Stock_price/full_history.zip -d .cache/raw/prices/`.

> If a download 401s, run `huggingface-cli login` with a free token first.

---

## 5. Build plan (`scripts/build_cpt_from_hf.py`)

**Inspect headers first** — print the CSV header and 2 rows before coding; column names can
drift. Expected shapes:

- **News CSV** columns (approx): `Date`, `Article_title`, `Stock_symbol`, `Url`, `Publisher`,
  `Author`, `Article` (full text), plus summary columns (`Lsa_summary`, `Luhn_summary`, …).
  Join on `Stock_symbol` + `Date`; use `Article` for the text (fall back to `Article_title`
  + a summary if `Article` is empty).
- **Price CSV** (`full_history/<TICKER>.csv`) columns (yfinance-style): `date`, `open`,
  `high`, `low`, `close`, `adj close`, `volume`. Use `close`.

Steps:
1. Stream the news CSV in chunks (`pandas.read_csv(..., chunksize=...)`) — it's multi-GB;
   do **not** load it whole.
2. Normalize `Date` → `YYYY-MM-DD` (drop intraday time). Group news by `(ticker, date)`.
3. **Dedup** within a `(ticker, date)`: many rows are the same wire article tagged to
   multiple tickers — dedup by normalized article text before concatenating. Cap combined
   text at `text.max_chars`.
4. For each `(ticker, date)` with news: load that ticker's price CSV, take the
   `history_days` closes **ending the trading day before** `date`. Skip if `< min_history_days`.
5. Build the record (§2). The closing sentence carries the single `<ts></ts>`.
6. **Sampling / scale control:** the full news set is far too large to emit in full; expose
   `output.max_records` and a per-ticker / per-month cap so the build is a deliberate sampled
   set, not everything. Log what you drop — no silent truncation.

---

## 6. Challenges & open questions — write these up in `CHALLENGES.md`

These are the calls that shape data quality and scale. Pick a sensible default for the first
build **and** flag each one for the team lead — don't bury them. For each, state what you chose,
why, and the alternative.

1. **Text–ticker attribution is noisy.** Many articles are broad market commentary (Fed, macro,
   sector moves) tagged to dozens of tickers, so the "news" often isn't about the paired ticker.
   *Options:* (a) keep all tagged pairs; (b) keep only articles whose title/body actually names
   the ticker or company; (c) keep only single-ticker articles. Trade-off: quality vs. volume.
2. **TS depth — single close channel is thin.** A short trailing close-only window is a weak
   time series (the team has been pushing for genuinely multi-step series elsewhere). *Options:*
   (a) longer window — 30/60 trading days instead of 7; (b) multi-channel OHLCV (open/high/low/
   close/volume, like the StockNet package) for a richer 5-channel series. Recommend raising
   both window length and channel count; confirm with the lead.
3. **Window semantics / no-lookahead.** Default: prices end the trading day **before** the news
   date (text = "today", series = run-up). Confirm this is the wanted layout vs. including the
   news-day close.
4. **Dedup of market-wide articles.** The same wire article appears under many tickers. Within a
   `(ticker, date)` we dedup by text — but should a market-wide article be emitted once per
   ticker at all, or collapsed? Affects how much near-duplicate text enters the corpus.
5. **Scale & sampling (15.7M news rows).** Emitting everything is neither feasible nor desirable.
   Decide the target size and sampling axis: ticker universe (e.g. S&P500 only), date range,
   per-ticker cap, per-month cap. Log exactly what's dropped.
6. **News-day gaps.** ~40% of trading days have no news for quieter tickers (≈38% coverage for
   `A` vs ≈82% for `AAL`). Days without news are simply not emitted — confirm that's acceptable.
7. **Multiple articles, same day.** Concatenate how many, in what order, capped at what length?
8. **License: CC BY-NC 4.0** — research use only. Confirm acceptable for the training corpus;
   note it in the run report.

---

## 7. Outputs, git hygiene, disk

- Output: `output/fnspid_cpt.jsonl` + `output/run_report.json` (counts, drops, config snapshot).
- `.cache/` is gitignored (repo `.gitignore` has `**/.cache/`). Put **all** raw downloads and
  extracted prices under `.cache/raw/`.
- **Never `git add` the multi-GB CSVs or a multi-GB JSONL.** If the full output is huge, commit
  only a small sample (`samples/example_output.jsonl`, ~50 lines) and document the full
  path/size in the README. The repo owner controls git — **do not commit or push** unless asked.

---

## 8. Validation checklist (run before declaring done)

For every emitted record:
- [ ] exactly one `<ts></ts>` in `text`
- [ ] `task_type == "world_knowledge"`, `text_quality == "real"`
- [ ] each `timeseries` channel: non-empty `values`, has `unit`, `freq == "1d"`
- [ ] `len(values) >= min_history_days`
- [ ] price window ends strictly **before** `news_date` (no lookahead)
- [ ] text length ≥ `min_news_chars` after dedup

Spot-check 5 random records by hand: does the news text plausibly precede the price series,
and is the ticker right?

---

## 9. Smoke test → full run

```bash
# 1. tiny smoke test on a couple tickers
python scripts/build_cpt_from_hf.py --set data.tickers_filter=[AAL,AAPL] --set output.max_records=20 --dry-run
# 2. capped real run
python scripts/build_cpt_from_hf.py --set output.max_records=5000
# 3. full (sampled) build
python scripts/build_cpt_from_hf.py --set output.max_records=null
```

When the format looks right, update `README.md` (status → built, real record count, the §4
source table) and write `output/run_report.json`.
