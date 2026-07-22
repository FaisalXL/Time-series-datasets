# Earnings-Call Transcripts + SEC XBRL Fundamentals → CPT

> **Status: Built** (demo: 50 records). Full build ~**25k+** records (transcripts from 2012 with ≥12 quarters of XBRL history). Run with `output.max_records=null`.

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

**Record shape:** (real — Deere & Company, Q2 2025; revenue/net-income arrays in USD, abbreviated)
```json
{
  "text": "Operator: Good morning, and welcome to Deere & Company's Second Quarter Earnings Conference Call... Trailing 12-quarter fundamentals (revenue, net income USD; diluted EPS) through fiscal Q2 2025: <ts></ts>",
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
- **⚠️ Overlap with the team's SEC EDGAR dataset — confirm before scaling.** The XBRL *fundamentals* side may duplicate that work; the *novel* element is the transcript↔fundamentals pairing. Get a quick "not redundant" sign-off from Charon before the full build.
- **Alignment = describes (strongest we have)** — the exec states revenue/EPS = the XBRL numbers. Leakage of the reported quarter's value into the text is inherent and *the point*.
- **Q4 / annual gap:** companies file Q4 inside the 10-K as a full-year figure, so there's **no standalone Q4 quarterly fact** — Q4-call windows end at Q3 (Q1–Q3 calls align exactly to the reported quarter). Optional fix: compute Q4 = annual − 9-month sum.
- **Environment:** reads the parquet with **duckdb** (no pandas/pyarrow), so it works even on Python 3.14; SEC access uses stdlib `urllib`.
- **SEC etiquette:** descriptive `User-Agent` required; `companyfacts` cached per CIK; ~0.15 s delay (SEC allows ~10 req/s).
- **Demo output ≈ 639 KB** for 50 long transcripts; lower `output.max_records` or `text.max_text_chars` for a more GitHub-friendly sample.

**Run:**
```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=5   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50)
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full build (~25k+)
```

**Output:** `output/earnings_calls_xbrl_cpt.jsonl` + `output/run_report.json` (`samples/` gitignored; `.cache/` holds the parquet + per-CIK companyfacts so reruns are free).

**Sources:** [Bose345/sp500_earnings_transcripts](https://huggingface.co/datasets/Bose345/sp500_earnings_transcripts) (MIT) · [SEC EDGAR XBRL](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) (public domain).
