# 60 — RBNZ Monetary Policy Statements → CPT world-knowledge records

**Status: DEMO BUILT + VERIFIED (2026-08-03). 1 record, 7 channels, 1/1 passes `validate.py --strict`.**
Awaiting Faisal's clear → then the server does the full archive run.

One record = an MPS's own **verbatim overview narrative** paired with a **7-channel macro series**
(OCR, unemployment, headline/tradables/non-tradables inflation, real GDP, TWI).

| | |
|---|---|
| Text | RBNZ Monetary Policy Statement web pages (HTML, no PDF parsing needed) |
| Series | RBNZ's own co-published "MPS data XLSX" (multi-sheet, ~55 sheets/tables) |
| License | RBNZ reproduction-with-attribution (custom grant; tagged `cc-by-4.0` closest-fit, real grant in `meta.true_license`, same pattern as INSEE #57) |
| Alignment | `recites` 1/1 (demo); 3/7 channels evidence-matched |
| Freq | `1q` (macro channels), `1d` (TWI) — mixed-frequency record |
| Depth | 24-quarter trailing window (macro), 90-day trailing window (TWI) |
| Demo | 1 statement (Feb 2026) → 1 record, 7 channels |

## Quickstart

```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --config config.example.yaml
python ../schema/validate.py output/rbnz_mps_cpt.jsonl --strict
```

---

## Access: rbnz.govt.nz is bot-walled; this build sources from Wayback instead

Direct fetches to `rbnz.govt.nz` returned **HTTP 403** during scouting (2026-08-03) — reads as a
bot-wall, not simple rate-limiting (deep-research5 separately flagged RBNZ's ~1 req/min bot limit,
consistent with this). **Verified working alternative:** Wayback Machine has live-crawled snapshots
of both the MPS page and its co-published data XLSX; this demo sources from those.

**Wayback itself is also flaky** — a genuinely confirmed finding, not a guess: the *exact same* XLSX
URL returned a valid 1.97MB file on one fetch, then a 10KB HTML placeholder ("nearest capture"
redirect landing on an uncaptured timestamp) on the next, with no code change. `fetch(expect_zip=True)`
now validates the ZIP signature (`PK` magic bytes) before accepting a binary response and retries
if it gets HTML instead — do not trust the first response for a binary asset from Wayback.

A full run should either commit to Wayback throughout (accept its flakiness, retry-guard every
binary fetch), or build a real headless-browser path paced to RBNZ's own stated ~1/min limit —
either is an access-method choice, not a license blocker (the reproduction grant is unambiguous).

## Real bugs caught during the build (four, all from checking output, not trusting the design)

1. **Header-row off-by-one silently fed unit labels in as column names.** Every sheet in the data
   pack has TWO header rows above the first date row — a LABEL row (`"Feb MPS"`/`"Nov MPS"`, or
   named sub-series like `"Headline"`/`"Non-tradables"`/`"Tradables"`) two rows up, then a UNITS
   row (`"%"`, `"Index"`, ...) one row up. The first cut read the units row as if it were the label
   row — harmless for single-column sheets, but it silently broke the 3-column inflation sheet
   entirely (zero channels emitted; column lookups by name failed against `"%"`/`"%"`/`"%"`).
2. **A cross-channel false-match manufactured fake alignment.** With a naive "does this channel's
   value appear anywhere in the text" search, OCR's Sep-2025 value (3.14, rounds to "3.1") matched
   the text's *"Inflation increased to 3.1%"* — crediting an inflation claim to the wrong channel
   purely by numeric coincidence. Same failure shape as GAIN's unit-word check. Fixed: each channel
   now requires one of its own keywords (`"OCR"`, `"unemployment"`, `"GDP"`, ...) within 60 characters
   *before* the matched number. The tradables/non-tradables pair needed a regex negative-lookbehind
   rather than a keyword list, since `"tradables inflation"` is a literal substring of
   `"non-tradables inflation"`.
3. **GDP was mislabeled as a growth rate.** Sheet 2.1 ("Production GDP") is a real **level** in
   chain-linked 2009/10 NZD billions (values ~49bn in 2008 rising to ~70–77bn by 2026), not a QoQ
   percent change as first assumed. Relabeled `production_gdp_real_2009_10_nzd_bn`.
4. **Leftover nav-link text in the narrative.** The extracted text included `"Download the MPS
   (PDF, 5 MB) Read the MPS online"` glued into the first sentence — link labels from the page
   chrome, not narrative. Stripped with a targeted pattern.

## The "current quarter" split — a real per-series distinction, not a detail

The data pack's own-vintage column runs from measured history straight into the Bank's own
projection with **no visual break**. Verified against this statement: the OCR row for the
statement's own quarter (Q1 2026, `01/03/2026`) = **2.25%** — exactly *"agreed to hold the Official
Cash Rate to 2.25%"*, a real current **decision**, not a forecast. But unemployment's and
inflation's same-quarter row is a genuine forward **projection**; the text's own quoted values
(unemployment 5.4%, inflation 3.1%) match the **prior** quarter (Q4 2025, the last actual outturn)
instead. All quarterly channels are windowed to the same end-quarter — required by the schema's
equal-length-per-freq rule — so for series where that final point is a projection, it's handled
under the same forecast-not-measured caveat already established for WASDE #41 / GAIN #58: the
terminal point is the Bank's own contemporaneous forecast, and the text is contemporaneous with it,
so there's no future-value leakage.

## Alignment is auditable — and honestly partial

`meta.recite_evidence` shows exactly 3 of 7 channels matched in this statement: OCR (2.25%,
current-quarter decision), unemployment (5.4%, prior-quarter actual), headline inflation (3.1%,
prior-quarter actual). GDP, TWI, and the tradables/non-tradables inflation sub-components are
present as legitimate context channels but aren't individually quoted with an exact number in *this*
statement's overview text — same "not every channel is individually recited" pattern already
established for WASDE #41's multi-channel balance sheets. Record-level alignment is `recites`
because at least one channel matched, not because every channel did.

## Scale

**Confirmed via Wayback CDX:** 117 total captures under the `monetary-policy-statement/` URL
prefix — consistent with deep-research's ~110-statement estimate (quarterly since ~1998–99).

**A real gap, not yet resolved:** the CDX sweep also surfaced a SECOND, OLDER URL scheme
(`.../monetary-policy-statement/mps-{month}-{year}`, e.g. `mps-december-2015`,
`mps-august-2016..2021`) distinct from the current `filtered-listing-page/{year}/{month-slug}/...`
pattern used by this demo — the same two-URL-generation shape already seen in FHFA #59. Whether the
older pages carry the same HTML-narrative-plus-XLSX combo, or are PDF-only (closer to GAIN #58's
shape), was **not confirmed** — a follow-up Wayback fetch to check hit archive.org's own rate limit
mid-session. This is a genuine to-do, not a dead end, in the same shape as GAIN's report-enumeration
gap and FHFA's pre-2021 reach-back gap.

## Known gaps / server to-dos

* **Older URL-scheme content format unconfirmed** (see "Scale") — check whether `mps-{month}-{year}`
  pages have the same HTML narrative + XLSX, or need PDF parsing instead.
* **Wayback flakiness needs a retry policy for every binary fetch**, not just the demo's single XLSX
  (see "Access").
* **Only the overview/summary section is parsed here.** The full MPS PDF has much deeper
  chapter-level content (Chapters 2–6 per the data pack's own Table of Contents) that could pair
  with the same series at finer granularity — headroom, not built.
* `datasets/README.md` row still needed (shared file — flagged, not edited here).

## Provenance

* Statement page: `rbnz.govt.nz/monetary-policy/monetary-policy-statement/monetary-policy-statement-filtered-listing-page/{year}/{month-slug}/...` (fetched via Wayback in this build; direct access 403s).
* Data pack: co-published `mps{mon}{yy}-data.xlsx` linked from the statement page (~55 sheets: OCR,
  GDP, unemployment, inflation components, TWI, interest rates, and more per the pack's own
  Contents sheet) — fetched via Wayback in this build.
* Demo statement: February 2026 MPS, published 2026-02-18.
