# 59 — FHFA House Price Index monthly reports → CPT world-knowledge records

**Status: DEMO BUILT + VERIFIED (2026-08-03). 2 records, 20 channels, 2/2 pass `validate.py --strict`.**
Awaiting Faisal's clear → then the server does the full archive run.

One record = a monthly HPI report's own **verbatim narrative** paired with the **10-channel index
series** (US + 9 census divisions) it discusses. Measured history only — no forecast language,
unlike WASDE #41 / GAIN #58, so no forecast-caveat is needed here.

| | |
|---|---|
| Text | FHFA monthly HPI report pages (plain HTML, no OCR/PDF needed) |
| Series | `hpi_master.csv` bulk file (184,827 rows), filtered to US + 9 divisions, monthly, SA |
| License | `public-domain-us-gov` — US-federal work product, 17 U.S.C. §105 |
| Alignment | `recites` 2/2 (demo); evidence-checked 5/6 and 6/6 |
| Freq | `1m` |
| Depth | 24-month trailing window per channel (WASDE precedent) |
| Demo | 2 reports (April + May 2026 index months) → 2 records, 10 channels each |

## Quickstart

```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --config config.example.yaml
python ../schema/validate.py output/fhfa_hpi_cpt.jsonl --strict
```

---

## The URL is not what it looks like — a 2-month publication lag (found 2026-08-03)

The `/reports/house-price-index/{year}/{month}` URL's `{month}` is the **publication** month, not
the index month it describes — confirmed empirically: the `2026/6` page's own narrative opens
*"U.S. house prices fell nationwide in April..."* (April data, published in June); `2026/7`
describes May data. **The rule is `url_month = index_month + 2`** (with year rollover for
Oct/Nov/Dec index months). The build script computes this automatically from the config's
`year`/`month` (which is the index month) — do not hand-construct the URL from the index month
directly, that was the first bug this build hit.

## Why the monthly report page, not the "news release" URL

The `fhfa.gov/news/news-release/...` URL is an unpredictable slugified headline (e.g.
`...-up-0.3-percent-in-may-up-2.2-percent-from-last-year`) — the same enumeration problem GAIN #58
hit. `fhfa.gov/reports/house-price-index/{year}/{month}` carries the **identical narrative**
(verified line-for-line) under a predictable URL, so a full run can construct URLs directly instead
of needing a report-list API.

**Caveat found while testing this pattern's depth:** it only resolves back to **~2021** (confirmed:
2021+ consistently 200, 2017–2020 consistently 404 across repeated retries — a real boundary, not
the access flakiness below). Reaching further back needs the older news-release URLs, which are
enumerable via Wayback CDX — see "Scale" below, same technique that solved INSEE #57's enumeration.

## Access flakiness (distinct from the above — a real but different finding)

`fhfa.gov` intermittently 404s on the *first* hit to a given path and 200s on retry — confirmed
non-deterministic across identical back-to-back requests to the *same* URL (e.g. `2020/6` flipped
200/404/200/200/404 across a 12-request sweep, no pattern by month). This looks like edge/WAF
flakiness, not a missing-page signal. `fetch()` retries up to 4× with backoff on a 404 before
raising. **Do not mistake this for the URL-boundary finding above** — the boundary (~2021) reproduced
consistently across many retries; the flakiness did not.

## Why monthly, not quarterly

The quarterly report page's own HTML text is thin — verified directly: 4 sentences, national-only
(*"U.S. house prices rose 1.7 percent between the first quarter of 2025 and the first quarter of
2026 ... House prices for the first quarter of 2026 rose 0.5 percent compared to the fourth quarter
of 2025."*). The division/state/metro breakdown lives in a **PDF attachment**, not the page text.
The monthly page carries real division-level prose directly in HTML (2 MoM extremes + 2 YoY
extremes + the national figures) — no PDF parsing needed. Quarterly + its PDF attachment is
headroom, not built here (see "Scale").

## Alignment is auditable, and evidence-checked against live data

`meta.recite_evidence` records each matched (place, metric, claimed %, computed %) pair. The May
2026 report matched **6/6**: national MoM/YoY plus all 4 named division extremes. April 2026
matched **5/6** — the Mountain division's claimed MoM (-0.8%) computes to **-0.97%** against the
current `hpi_master.csv`, a real ~0.17pp drift since the report's June publication (not a bug — see
"Vintage-revision risk" below). The alignment threshold (`matched >= total - 1`) tolerates exactly
this kind of small, expected drift without being loose enough to mask a real mismatch.

## Vintage-revision risk — confirmed, not hypothetical

FHFA's repeat-sales index revises its own recent history every release, and often **says so in the
text itself**: *"The previously reported 0.1 percent price change in March was revised upward to
0.2 percent."* This build parses that sentence (`meta.revision_sentence_check`) and checks it
against current data — and on the April 2026 report, the live value has drifted **again**: the
current `hpi_master.csv` shows March's MoM at **0.13%** (rounds to 0.1%), matching *neither* the
originally-reported 0.1% *nor* the "revised to 0.2%" figure the text states.
`still_matches_revised_figure: false`. This is expected behavior for the index, not an error, and
it isn't the GAIN-style self-contradiction (no superlative claim is being refuted by a value in the
same shipped series) — but it's real evidence that **a full historical run needs per-release
vintage archiving**, not live `hpi_master.csv`, to keep every stated figure exactly matched. Same
discipline already established for `ons_awe` and GAIN #58's PSD splice.

## Superlative guard carried over from #58 (cheap insurance, not yet triggered)

`check_superlatives()` scans for highest/lowest/record language the same way #58's guard does.
Neither sampled release used it (2 monthly + 1 quarterly release checked directly for superlative
language — none found), and FHFA's formulaic MoM/YoY style makes it less likely than GAIN's
multi-year narrative reports. Kept active (`drop_on_superlative_contradiction: true`) as insurance
for whichever historical release eventually does use such language (e.g. during a housing
boom/bust), not because it fired in this demo.

## The deep-research "flywheel" claim did NOT hold up — checked directly, not assumed

The original scouting pass claimed FHFA publishes *"quarterly state/metro 'Highlights' articles"*
as first-party narrative text pairable with the rich sub-state bare series (50 states, 400+ metros,
counties, ZIPs, tracts). **Checked directly on fhfa.gov/data/hpi: no such FHFA-authored narrative
exists.** What surfaces under that description is **third-party** commentary (e.g. NAHB's "Eye On
Housing" blog interpreting FHFA's numbers) — not first-party text, and likely not redistribution-safe
at that tier. **The sub-state series remain a legitimate BARE-SERIES flywheel candidate** (rich,
real, high-cardinality data) for Oliver's engine to pair with *separately retrieved* leakage-safe
text — but they are not a directly-buildable first-party addition the way the deep-research pitch
implied. Flagging this so the scale story doesn't quietly inherit an unverified claim.

## Scale

**Directly buildable now (clean, enumerable pattern):** ~2021–present monthly reports ≈ 66 reports
× 1 record/report = **~66 records**. Well under the >10k bar alone — this source is diversity/
correctness-verified tier at this ceiling, not a scale play by itself.

**Recoverable with more engineering (a real path, not a guess):** Wayback CDX enumeration of the
older `news-release` slug URLs — same technique that solved INSEE #57's JS-SPA enumeration problem.
A single CDX query (`fhfa.gov/news/news-release/`, prefix match, filtered to percent-bearing slugs)
already surfaced **166 distinct historical release captures** without exhausting the archive. Slugs
don't carry the release year, so the exact historical floor (level with the underlying index data,
which itself goes to 1991 monthly / 1975 quarterly all-transactions) needs a follow-up pass that
fetches each candidate to read its real publish date — a genuine to-do, not a blocker, in the same
shape as GAIN's report-enumeration gap. If that reach-back lands, the original ~5k estimate
(10 geographies × ~420 monthly releases + quarterly) is plausible; **do not assume it without the
follow-up**, given how GAIN's own pre-verification estimate needed correcting.

**Quarterly + PDF attachment headroom:** the quarterly release's PDF attachment reportedly carries
state/metro/county tables (per its own text: *"Tables and graphs showing home price statistics for
metropolitan areas, states, census divisions, and the U.S. are included in the attachment"*) — not
yet opened/parsed. If it contains genuine per-geography prose (not just tables), this is a second
reach-back path; if it's tables-only, it's not usable per this corpus's "no fake scale" /
table-dump-only exclusion (same class as the killed BOJ Tankan).

## Known gaps / server to-dos

* **Enumeration reach-back unresolved** (see "Scale") — the biggest scale lever, needs the CDX
  follow-up pass.
* **Per-release vintage archiving** needed for a historically-accurate full run (see
  "Vintage-revision risk").
* **Quarterly PDF attachment unopened** — check whether it's prose or tables-only before deciding
  if it's buildable headroom.
* `datasets/README.md` row still needed (shared file — flagged, not edited here).

## Provenance

* Reports: `https://www.fhfa.gov/reports/house-price-index/{year}/{month}` where
  `month = index_month + 2` (see "publication lag" above).
* Series: `https://www.fhfa.gov/hpi/download/monthly/hpi_master.csv` — keyless bulk CSV,
  `hpi_type × hpi_flavor × frequency × level × place_name × yr × period → index_sa` (this build
  uses `traditional` / `purchase-only` / `monthly` / `USA or Census Division`, matching what the
  narrative actually recites).
* Demo reports: April 2026 index month (page `2026/6`, published 2026-06-30) and May 2026 index
  month (page `2026/7`, published 2026-07-28).
