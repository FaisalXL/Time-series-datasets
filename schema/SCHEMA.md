# CPT World-Knowledge Record Schema — v1

*Status: **finalized for freeze** · Owner: Defu · Last updated: 2026-07-13*
*Supersedes the informal "Target record shape" in [`../AGENT_BRIEF.md`](../AGENT_BRIEF.md). The four required fields are unchanged; this document formalizes them and adds optional provenance, alignment, and multi-series fields.*

---

## 0. What this schema is for

We are assembling a **continued / mid-pre-training (CPT) corpus** for a joint text + time-series foundation model. The training objective of this stage is **world-knowledge injection**: the model should absorb *how the world behaves* — that a Kp index of 7 means a geomagnetic storm, that a flu season peaks in winter, that revenue and EPS move together — by reading real official text that is *natively aligned* to the numbers describing the same phenomenon.

This is **not** instruction tuning. Records carry no prompt/response, no task instruction, no question. Each record is a self-contained *statement about the world* in which one modality (prose) explains the other (series). During training the `<ts></ts>` token is where the encoded series is spliced into the text stream.

Everything below serves one rule, stated once and applied everywhere:

> **The alignment rule (Charon's core rule).** The text must genuinely describe the same real-world phenomenon that the numbers represent, at a granularity the source itself imposes. No arbitrary sliding windows, no metadata dressed up as language grounding, no synthetic scale.

---

## 0.1 Files in this folder

| File | Role |
|------|------|
| `SCHEMA.md` | This document — the human-readable spec, qualification rules, and rationale. |
| `cpt_record.schema.json` | Machine JSON Schema (Draft 2020-12). Encodes the **strict** target: required fields **and** the recommended optional vocab as hard enums. Equivalent to `validate.py --strict`. |
| `validate.py` | Linter. **Default:** enforces only the required contract (existing packages pass — verified 1786/1786). **`--strict`:** promotes optional-vocab drift to errors, matching the JSON Schema. Run `--strict` on every new package. |

So an existing package that "fails" the raw JSON Schema is not broken — it simply predates the standardized `source`/`license`/`text_source` vocab, which is a migration item, not a freeze blocker.

## 1. Record shape (canonical example)

Each line of `output/*_cpt.jsonl` is exactly one JSON object.

```json
{
  "text": "Key Points: During week 40 (October 4-10, 2015), influenza activity was low in the United States. The weighted percentage of visits for influenza-like illness was <ts></ts>.",
  "timeseries": [
    {"values": [1.22559], "unit": "ili_pct_weighted", "freq": "1w"},
    {"values": [10049.0], "unit": "ili_total_visits", "freq": "1w"}
  ],
  "task_type": "world_knowledge",
  "text_quality": "real",

  "series_id": "cdc_fluview:2015:w40",
  "dataset": "cdc_fluview",
  "source": "https://www.cdc.gov/flu/weekly/weeklyarchives2015-2016/week40.htm",
  "license": "public-domain-us-gov",
  "text_source": "first_party_official",
  "alignment": "recites",
  "domain": "public_health",
  "region": "US",
  "period_start": "2015-10-04",
  "period_end": "2015-10-10",
  "meta": {"season": "2015-2016", "week": 40}
}
```

---

## 2. Required fields

These four fields **must** appear in every record. A record missing any of them is invalid and must be skipped by the build script (counted in `run_report.json`).

| Field | Type | Rule |
|-------|------|------|
| `text` | string | Natural prose only — no task instructions, no Q/A framing. Contains **exactly one** `<ts></ts>` placeholder (see §4 for the multi-series exception). Non-empty and above the package's `min_text_chars`. |
| `timeseries` | array of channel objects | One or more channels. Each channel is `{values, unit, freq}` (see §3). Channels **may** differ in length and frequency (mixed-frequency records are legitimate — e.g. a daily scalar alongside an 8-step 3-hourly series). Channels that **share a `freq`** must share a length and are index-aligned. |
| `task_type` | string enum | Always `"world_knowledge"` for this corpus. Reserved for future CPT sub-objectives; do not invent values. |
| `text_quality` | string enum | `"real"` — first-party human/official text. `"generated"` — model-synthesized text, allowed **only** with team sign-off and always tagged. No other values. |

### 2.1 Channel object (inside `timeseries`)

| Field | Type | Rule |
|-------|------|------|
| `values` | array of number | Numeric observations in time order. May contain `null` for genuine gaps (do not fabricate). Length ≥ 1. Must equal the length of any other channel sharing the same `freq`; may differ from channels at other frequencies. |
| `unit` | string | Machine-readable channel label + unit, `snake_case`, e.g. `ili_pct_weighted`, `close_price_usd`, `stage_ft`, `revenue_usd`, `events/day`. Distinct labels are **recommended** but not required — a physical unit (e.g. `thousand_barrels`) may legitimately recur across several channels. |
| `freq` | string | Compact interval token (see §3.2). |

---

## 3. Field conventions

### 3.1 The `<ts></ts>` placeholder

Marks where the encoded series is consumed during CPT. Conventions:

- **Exactly one** per record (multi-series exception in §4).
- Prefer placing it in a short closing/summary sentence that *names what the series is* ("…the weighted ILI percentage was `<ts></ts>`."), not mid-paragraph.
- The placeholder replaces the number(s), it does not annotate them — the text should read naturally if a human substituted the series back in.
- Empty tag only: `<ts></ts>`. No attributes, no index, no whitespace inside.

### 3.2 `freq` — compact interval tokens

Format: `<integer><unit>` where unit is one of the tokens below. Case matters where it disambiguates (`1M` month vs `100ms` milliseconds).

| Token | Meaning | Token | Meaning |
|-------|---------|-------|---------|
| `100ms` | 100 milliseconds | `1w` / `1W` | 1 week |
| `3h`, `6h` | hours | `1M` | 1 month |
| `1h` | 1 hour | `1q` | 1 quarter |
| `1d` | 1 day | `1y` | 1 year |
| `1m` | 1 minute | `1over` | 1 cricket over (domain-native epoch) |

Domain-native epochs (`1over`, and future analogues) are allowed when the source's natural sampling unit is not clock time. Document any new token in the package README **and** add it to the `FREQ_RE` pattern in `validate.py`.

**Mixed-frequency records.** A single record may bundle channels at different frequencies (e.g. NOAA SWPC daily = a `1d` scalar plus a `3h` 8-step series). This is expected and remains **single-`<ts>`**: the one splice point consumes the whole channel bundle. Channels at the *same* frequency must be equal length and index-aligned; channels at *different* frequencies may differ in length.

### 3.3 Timestamps (optional but recommended for irregular series)

If sampling is irregular, or absolute time matters for the phenomenon, include a top-level `timestamps` array (ISO-8601 strings) parallel to `values` — same length as the channels. Regular series may omit it and rely on `freq` + `period_start`.

---

## 4. Multi-series records (`<ts>` count > 1)

Some phenomena are best described by prose that references **two or more distinct series in different places** (e.g. "GDP `<ts></ts>` rose while unemployment `<ts></ts>` fell"). This is permitted under a strict contract:

- The number of `<ts></ts>` placeholders **must equal** the length of `timeseries`, and they bind **in order**: the *k*-th `<ts></ts>` consumes `timeseries[k]`.
- In this mode, channels are **not** required to share a length (each `<ts>` is an independent series).
- Set `"multi_series": true` at the top level to signal the alternate contract to the validator.
- Default and strongly preferred mode remains **single `<ts>`, multi-channel** (all channels index-aligned, one splice point). Only use multi-series when the source text genuinely interleaves independent series.

---

## 5. Optional metadata fields (recommended, standardized)

Beyond the four required fields, use these **standardized** optional keys so records are queryable and de-dupable across packages. Additional dataset-specific keys are allowed but should be nested under `meta` (§5.1) to keep the top level stable.

| Field | Type | Purpose |
|-------|------|---------|
| `series_id` | string | Stable unique id for this record, `dataset:scope` form, e.g. `cdc_fluview:2015:w40`. Enables dedup and provenance. |
| `dataset` | string | Package slug, e.g. `cdc_fluview`, matches the `NN_<slug>` folder. |
| `source` | string (URL) | Canonical URL of the originating report/filing/page. |
| `license` | string enum | See §6. |
| `text_source` | string enum | `first_party_official`, `first_party_human`, `third_party`, `generated`. Distinguishes who authored the prose (an official CDC report vs. investor tweets vs. GPT). |
| `alignment` | string enum | How text relates to numbers — see §7. |
| `domain` | string | Coarse topic, e.g. `public_health`, `finance`, `meteorology`, `space_weather`, `macro_econ`, `sports`. |
| `region` | string | Geographic scope, e.g. `US`, `US-ND`, `global`, `Arctic`. |
| `period_start` / `period_end` | string (ISO-8601 date/time) | Real-world time span the record covers. |
| `meta` | object | Free-form dataset-specific extras (season, week, ticker, gauge id, fiscal_quarter, …). |

### 5.1 Why `meta` exists

The current packages scatter dataset-specific keys at the top level (`season`, `ticker`, `gauge_lid`, `crest_ft`, …). That works but makes the top-level shape unpredictable. **Going forward, nest dataset-specific keys under `meta`.** Existing packages need not be rewritten for the freeze — the validator accepts top-level extras — but new packages should use `meta`.

---

## 6. Licensing (`license`)

Every record should declare its license so the corpus can be filtered at training time. Use one of:

| Value | Meaning |
|-------|---------|
| `public-domain-us-gov` | US federal government work (NOAA, CDC, BLS, EIA, Fed, USGS). Default for most packages. |
| `cc-by-4.0` | Creative Commons Attribution (e.g. ICS-209-PLUS). Attribution retained in `source`. |
| `cc0` | Public-domain dedication. |
| `proprietary-review` | Copyrighted/third-party text pending redistribution sign-off (e.g. ESPNcricinfo reports, GPT-generated text). **Excluded from any release until cleared.** |
| `unknown` | License not yet determined. Treated as `proprietary-review` for release purposes — must be resolved before scaling. |

---

## 7. Alignment taxonomy (`alignment`) — the qualification backbone

This is the field that operationalizes "what data qualifies." Each record declares *how* its text is aligned to its series. The three tiers, strongest to weakest:

| Value | Definition | Example | Qualifies? |
|-------|-----------|---------|-----------|
| `recites` | The text literally states the numbers that are the series. Text and series are the *same facts* in two modalities. | A CPI release stating the index values that are the `revenue_usd` channel; a Fed survey reciting its diffusion indices. | **Strongest.** Always qualifies. |
| `describes` | The text narrates the phenomenon the series measures without quoting every value — it characterizes shape, events, or thresholds. | A storm narrative paired with daily damage/injury counts; a flood report describing a crest paired with the hourly stage hydrograph. | **Qualifies** if the description is specific to *this* series (not boilerplate). |
| `contextualizes` | The text is contemporaneous commentary about the entity the series tracks, but authored independently (third-party). | Tweets/news about a ticker paired with that week's OHLCV. | **Qualifies with care** — flag `text_source: third_party`; watch relevance/noise; never the majority of the corpus. |

**Disqualified (do not build):**

- **Metadata-only grounding** — text that only names IDs, coordinates, or column headers, with no narrative about the phenomenon.
- **Boilerplate reuse** — the same template repeated across many windows to inflate counts (violates "no fake scale").
- **Arbitrary windows** — fixed stride-1 sliding windows imposed by us rather than by the source's reporting structure.
- **Semi-synthetic corpora** — machine-generated text/series pairs presented as real (e.g. CAF-7M is out). Genuinely generated text is allowed only as `text_quality: generated` + sign-off, and is a small, tagged minority.
- **SFT overlap** — sources already in the ~2M-row SFT corpus (check before adding).

---

## 8. Qualification checklist (per source, before building)

A candidate source qualifies for the CPT corpus if **all** of these hold:

1. **Real first-party (or clearly-tagged) text.** Official reports, advisories, filings, transcripts. Third-party commentary allowed as a tagged minority; generated text only with sign-off.
2. **Native alignment.** Text and series describe the *same* phenomenon at the *source's own* granularity (episode, storm, week, filing, over…). Classify it as `recites` / `describes` / `contextualizes`.
3. **Numbers are genuinely present as a series.** Not a single scalar dressed as a series unless that scalar is the natural unit (e.g. a monthly release = one step of a rolling window).
4. **Licensable for training.** Public-domain or permissively licensed, or flagged `proprietary-review` and quarantined.
5. **No SFT overlap.** Verified against the SFT corpus.
6. **Scale is honest.** Full-build count comes from real distinct source units, not template inflation.
7. **Documented.** Package README states record = one *what*, scale estimate, alignment type, and any open issues.

If a source fails 1–5 it is **out**. If it fails 6–7 it is **not ready** (fixable).

---

## 9. Validation contract (enforced by `validate.py`)

Every record in `output/*_cpt.jsonl` must pass:

1. Valid JSON object, one per line.
2. Required fields present with correct types: `text` (str), `timeseries` (non-empty array), `task_type == "world_knowledge"`, `text_quality in {real, generated}`.
3. `<ts></ts>` count == 1 (or == `len(timeseries)` when `multi_series: true`).
4. Each channel has `values` (non-empty numeric array, `null` allowed), `unit` (non-empty str), `freq` (matches `^\d+(ms|m|h|d|w|W|M|q|y|over)$`).
5. Channels sharing a `freq` share a length (mixed-frequency channels may differ in length).
6. `timestamps` (if present) length matches the reference channel length.
7. `text` length ≥ `--min-text-chars` (default 1; set per package).

**Warnings (not failures unless `--strict`):** `text_quality` aside, the optional vocab fields — `text_source`, `alignment`, `license` outside their recommended enums; `source` not a URL; a `unit` repeated within a record. Existing packages predate the standardized vocab and pass by default; **new packages should run `--strict` and clear all warnings.**

The validator reports per-file pass/fail/warn counts and the first N issues with reasons. It exits non-zero if any record fails (errors, or warnings under `--strict`), so it can gate CI / a pre-freeze check. Verified against the current dev set: **1786/1786 records pass the required contract.**

---

## 10. Migration notes (current packages → v1)

- The **four required fields already match** every built package — no rework needed for the freeze.
- Top-level dataset-specific keys (`season`, `ticker`, `crest_ft`, …) remain **valid**; new packages should nest them under `meta`.
- Add `alignment` and `license` to new packages from the start; backfilling existing ones is a cheap follow-up (most are `public-domain-us-gov`; alignment is listed per-package in `../README.md`).
- `text_source` lets us finally distinguish StockNet tweets (`third_party`) and TelecomTS anomaly text (`generated`) from official releases without changing `text_quality` semantics.

---

## 11. Open questions for the Oliver meeting

1. **Multi-series contract** — do we want it in v1, or defer and keep every record single-`<ts>`? (Simpler for the encoder; loses some natural macro/finance text.)
2. **`contextualizes` cap** — what fraction of the corpus may be third-party commentary before it dilutes world-knowledge signal?
3. **`meta` migration** — enforce nesting now, or accept top-level extras through the freeze and migrate later?
4. **`generated` policy** — final rule for TelecomTS-style synthetic narrative: include tagged, or exclude from v1?
5. **Series length / padding** — do we cap channel length (very long hydrographs, 128-step telecom windows) or leave to the encoder?
