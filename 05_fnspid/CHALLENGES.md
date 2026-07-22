# FNSPID → CPT: Challenges & Open Questions

Design decisions and open calls for the FNSPID CPT build. Each item states **what
we chose** for the first build, **why**, the **alternative**, and what to **confirm
with the lead** before scaling. Part A covers the questions the team raised directly
(irregular series, multivariate channels, cross-stock alignment). Part B covers the
data-quality / scale calls from the build brief.

Record contract recap: one record = one `(ticker, news_date)` — the real news
article(s) published that day, paired with that ticker's **trailing daily OHLCV
window ending the trading day before the news date** (no lookahead). See
`config.example.yaml` for every knob named below.

---

## Part A — Questions raised by the team

### A1. Irregular time series — the market is closed on weekends/holidays

**Context.** This is the irregularity the team flagged (and the focus of the
*Time-IMM* paper, arXiv:2506.10412 — "Irregular Multimodal Multivariate Time
Series"). Daily stock data only exists on **trading days**: weekends, holidays,
and halts leave gaps. On the calendar the series is *not* regularly sampled. In
Time-IMM's taxonomy this is **constraint-based** irregularity (a market-calendar
constraint), as opposed to trigger-based (event bursts) or artifact-based (sensor
dropouts).

**What we chose.** Keep the **real trading-day observations only — no imputation**,
and make the irregularity *explicit* rather than hidden:

- Each record carries a `trading_dates` array (ISO dates) **aligned 1:1 with the
  channel `values`**, so the exact calendar position — and every gap — is
  recoverable downstream.
- `freq` is labelled `"1d"` because the *native* cadence is one-business-day; the
  `trading_dates` array is the source of truth for the actual spacing.
- We do **not** resample to a 7-day calendar grid and we do **not** forward-fill or
  zero-fill weekends/holidays.

**Why.** This matches Time-IMM's core finding: forcing irregular data onto a regular
grid (or imputing) destroys real structure, whereas explicitly modelling the
timestamps improves downstream forecasting. For a CPT corpus labelled
`text_quality: "real"`, injecting synthetic weekend prices would be fabricated data —
the opposite of what we want. Carrying timestamps lets a timestamp-aware model (e.g.
IMM-TSF's timestamp-to-text fusion) learn the cadence; a timestamp-agnostic model can
simply ignore `trading_dates` and read the values as a regular trading-day sequence.

**Alternatives & trade-offs.**
- *(a) Implicit regular grid (values only, drop dates).* Simplest, but throws away the
  calendar — the model can't tell a Mon→Tue step from a Fri→Mon step. We keep the
  dates instead, at a small size cost.
- *(b) Resample to calendar days + impute (ffill / NaN / 0).* Makes spacing uniform
  but fabricates non-trading values; breaks the "real" contract and biases volume.
  Rejected.
- *(c) Encode an explicit gap/delta channel* (days-since-previous-observation). A
  middle ground; recoverable from `trading_dates` anyway, so we left it out for now.

**Confirm with lead.** Is carrying `trading_dates` + `freq:"1d"` the representation we
want repo-wide, or should irregular-cadence datasets use a distinct `freq` token
(e.g. `"1b"` for business-day) and/or a standard `timestamps` field in the contract?
This is a contract-level decision affecting every financial dataset.

### A2. Multivariate channels — use all of OHLCV, not just close

**Context.** Faisal: the series is multivariate (`open, high, low, close, adj_close,
volume`); the SFT build used only `close` for forecasting, but all are usable.

**What we chose.** Emit **all six channels by default** —
`open, high, low, close, adj_close, volume` — as separate entries in the
`timeseries` list, all sharing the same `trading_dates` index. Channels are a config
knob (`data.channels`), so a close-only or OHLC-only build is a one-line override.
The single `<ts></ts>` placeholder represents the whole multivariate block (the
contract requires *exactly one* `<ts></ts>`, not one per channel).

**Why.** Richer, genuinely multivariate series — the brief (§6.2) and the team both
want to move away from thin close-only windows. Each channel keeps its own `unit`
(`*_price_usd` vs `volume_shares`) so the very different volume scale is explicit and
not normalised away.

**Open points to confirm.**
- **`adj_close` vs `close`:** we include both. `close` is the nominal traded price
  (matches the news narrative); `adj_close` is split/dividend-adjusted (better for
  continuity across corporate actions but is a *rewritten* history). Keep both, or
  drop one to avoid a near-duplicate channel?
- **Per-channel vs joint scaling** is left to the training pipeline; we emit raw
  values. Confirm that's expected.

### A3. Cross-stock alignment — "some stocks aren't trading at time *t*"

**Context.** Defu_Cao / Xinyue: a stock's series is multivariate, but **each text
corresponds to one stock**. The hard cross-sectional problem is when a *universe* of
stocks is on one panel and some aren't trading at a given time (halts, IPO/listing
dates, delistings, differing holidays) → a **ragged panel**.

**What we chose — and why it sidesteps the problem.** Our record is **one ticker per
record**. The text describes a single company and the series is *that one ticker's*
history. So **there is no cross-sectional panel inside a record** — the
"stock B isn't trading when stock A is" misalignment **cannot arise** in the current
design. The only irregularity left is the per-series calendar gap handled in §A1.

**When the problem *would* return (and how we'd handle it).** If we later build
**market-panel records** — e.g. a macro/market-wide article paired with many tickers
at once (relevant because ~? of FNSPID articles are broad-market commentary, see B1/B4)
— the ragged-panel issue appears. Options for that future build:
1. **Common-calendar intersection:** keep only dates on which *all* universe members
   traded. Clean rectangle, but drops data and can't represent listings/delistings.
2. **Union calendar + explicit missingness:** one date axis = union of all trading
   days; per ticker, mark non-traded dates as missing (mask), never imputed. Most
   faithful, and the Time-IMM-aligned choice; needs a mask convention in the contract.
3. **Per-series irregular timestamps (no shared axis):** each ticker keeps its own
   `trading_dates`; fusion happens by timestamp at train time (IMM-TSF style). Most
   flexible, heaviest on the modelling side.

**Confirm with lead.** Do we stay one-ticker-per-record (problem avoided), or do we
*also* want market-panel records? If the latter, which alignment convention (we'd
recommend **#2, union + mask**) and we'd need to extend the record contract to carry
a per-channel missingness mask.

---

## Part B — Data-quality & scale decisions (build brief §6)

### B1. Text–ticker attribution is noisy
Many articles are broad market/macro commentary tagged to many tickers, so the "news"
often isn't really about the paired ticker. (Real examples from the build: ticker
`AACG`, a Chinese ed-tech, paired with a *precious-metals* article; the ETF `AADR`
paired with an *Itau Unibanco* dividend piece; `AAL` paired with an *S&P 500 options*
round-up.)
- **Chosen (two complementary levers):**
  1. A cheap **symbol heuristic** in the build (`filters.require_symbol_in_text`,
     default off) that requires the title/body to name the ticker (`(NASDAQ: AAL)`,
     `$AAL`, whole-word multi-char symbols).
  2. An optional **LLM relevance filter** as a post-build stage
     (`scripts/filter_news_relevance.py`) — a local model used as a **judge** that
     drops articles which are broad-market commentary rather than about the ticker.
     It **keeps the real text unchanged** (no rewriting → stays `text_quality:"real"`)
     and is shown **only the article + ticker, never the price outcome** (no leakage).
     This was the team's idea and is the recommended quality lever at scale.
     - **Empirical result:** judged the 5,000-record build with Gemma4-31b
       (`ds-serv10:8004-8007`, 4 lanes, ~9 min): **kept 2,723 / 5,000 (54.5%)**, avg
       confidence 0.99. So **~46% of even the symbol-tagged records were attribution
       noise** — sector round-ups, market lists, or articles about a *different*
       ticker (e.g. `ABT`→Avantor, `ACB`→Canopy Growth, `AADR`→Itau Unibanco). This
       quantifies how severe B1 is and how much the judge improves corpus quality.
- **Why not abstractive summarisation:** rewriting the news with an LLM would make the
  text synthetic; if wanted, it must be a separate, clearly-labelled track that also
  keeps the raw text. Extractive/selective filtering preserves the "real" contract.
- **Why not align text to the price move:** selecting news by how the price *reacted*
  is label leakage / lookahead — the judge is conditioned only on article content.
- **Confirm:** default the relevance filter **on** for training quality? confidence
  threshold? a company-name lookup table would further sharpen the symbol heuristic.

### B2. TS depth — close-only is thin → multivariate + longer window
- **Chosen:** `history_days: 30` (was 7 in the demo) **and** 6-channel OHLCV (§A2).
- **Why:** addresses the team's push for genuinely multi-step, multivariate series.
- **Confirm:** is 30 trading days the target, or longer (60/120)? Window length is a knob.

### B3. Window semantics / no-lookahead
- **Chosen:** the price window ends the trading day **strictly before** `news_date`
  (text = "today", series = the run-up). Enforced and unit-checked in validation.
- **Confirm:** is "history up to yesterday" the wanted layout, or should the news-day
  close be included?

### B4. Dedup of market-wide articles
The same wire article appears under many tickers.
- **Chosen:** dedup by normalised article text **within** each `(ticker, date)` group;
  cap concatenated text at `text.max_chars` (3000).
- **Open:** we do **not** yet dedup the *same* article emitted across *different*
  tickers — a market-wide piece can still enter the corpus once per ticker. With
  one-ticker-per-record that's arguably fine (each is a distinct (ticker, series) pair),
  but it does inflate near-duplicate text. **Confirm:** collapse cross-ticker
  duplicates, or keep one per ticker?

### B5. Scale & sampling (~15.5M news rows; ~3.74M have full article bodies)
Emitting everything is neither feasible nor desirable.
- **Chosen:** deliberate, logged sampling — `sampling.max_per_ticker` (40),
  `sampling.max_per_ticker_month` (4), optional `tickers_filter` / `date_min/max`, and a
  final seeded down-sample to `output.max_records`. Every drop reason is counted in
  `run_report.json` (no silent truncation).
- **Sampling-bias note:** the raw CSV is ordered by symbol then date-descending, so the
  per-ticker cap keeps each ticker's **most recent** dates first — a recency bias worth
  being aware of. The final `output.max_records` down-sample is random (seeded) to keep
  the emitted set representative across tickers.
- **Confirm:** target corpus size and sampling axes — full ticker universe vs S&P500
  only? date range? per-ticker / per-month caps as set?

### B6. News-day gaps (days without news simply aren't emitted)
- **Chosen:** we only emit `(ticker, date)` pairs that have news; quiet days are absent.
- **Confirm:** acceptable (it follows from the contract), per the brief.

### B7. Multiple articles, same day
- **Chosen:** concatenate up to `filters.max_articles_per_record` (5) unique articles,
  in CSV order, capped at `text.max_chars`; the true count is kept in `n_articles`
  (`n_articles_shown` records how many were inlined).
- **Confirm:** 5 articles / 3000 chars the right budget? Order by recency or relevance?

### B8. License — CC BY-NC 4.0 (research use only)
- **Chosen:** stamped on every record (`license`) and in `run_report.json`.
- **Confirm:** is non-commercial, research-only source text acceptable in the training
  corpus? This is a go/no-go the lead must sign off on.

---

## Summary of recommended defaults to confirm

| # | Decision | First-build default | Recommendation to lead |
|---|----------|---------------------|------------------------|
| A1 | Irregular cadence | real trading days + `trading_dates`, no imputation | adopt repo-wide; decide `freq` token / contract `timestamps` |
| A2 | Channels | 6-ch OHLCV+adj_close | keep multivariate; decide close vs adj_close |
| A3 | Cross-stock panel | one ticker/record (problem avoided) | add market-panel records only if needed → union+mask |
| B1 | Attribution filter | off (keep all), gate available | turn **on** for quality; consider name lookup |
| B2 | Window | 30 trading days | confirm 30 vs 60/120 |
| B3 | No-lookahead | window ends day before news | confirm |
| B4 | Cross-ticker dup | not collapsed | decide collapse vs keep |
| B5 | Scale/sampling | caps + seeded down-sample, all drops logged | set target size & universe |
| B8 | License | CC BY-NC 4.0, research only | **go/no-go** for training use |
