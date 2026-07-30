# 58 — USDA FAS GAIN attaché reports → CPT world-knowledge records

**Status: DEMO BUILT + VERIFIED (2026-07-30). 5 records, 56 channels, 5/5 pass `validate.py --strict`.**
Awaiting Faisal's clear → then the server does the full archive run.

One record = a GAIN report's own **verbatim narrative** paired with the **multi-channel PSD balance
sheet** that narrative discusses. GAIN is the *country-granular* sibling of built **WASDE #41**:
same PSD backbone, but #41 builds only `U.S. ...` tables, so foreign-post series here are
structurally net-new.

| | |
|---|---|
| Text | USDA FAS GAIN attaché reports (PDF, digital/text-extractable — no OCR) |
| Series | PSD Online bulk CSV (`apps.fas.usda.gov/psdonline/downloads/psd_<group>_csv.zip`) |
| License | `public-domain-us-gov` — US-federal work product, 17 U.S.C. §105 |
| Alignment | `recites` 4/5 · `describes` 1/5 (demo) |
| Freq | `1y` (PSD market years) |
| Depth | **40–67 annual points** per channel (Mexico cattle 1960→2026; Ukraine oilseeds 1987→2026) |
| Demo | 2 reports → 5 records, 7–18 channels each |

## Quickstart

```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --config config.example.yaml
python ../schema/validate.py output/fas_gain_cpt.jsonl --strict
```

Downloads are cached under `.cache/` (git-ignored): GAIN PDFs and the PSD bulk zips.

---

## The vintage splice (the core design — do not simplify this away)

PSD Online carries decades of annual history, but it always serves the **current** vintage, and
USDA keeps revising the recent/forecast years *after* a report ships. Measured on `MX2026-0012`:

| attribute | MY | report "USDA Official" | report "New Post" | live PSD today |
|---|---|---|---|---|
| Beginning Stocks | 2024 | 17,840 | 17,840 | 17,840 ✅ settled |
| Total Slaughter | 2024 | 7,050 | 7,050 | 7,050 ✅ settled |
| Total Slaughter | 2025 | 6,875 | 6,860 | 6,860 → Post's revision **adopted** |
| Total Slaughter | 2026 | 7,200 | 7,225 | **7,550** → drifted past *both* |
| Ending Stocks | 2026 | 19,740 | 19,835 | **19,510** → drifted past *both* |

Settled years matched **8/8**; the forecast year — *the year the prose is actually about* — had
drifted past both of the report's own columns. So:

> **series = PSD bulk for settled market years + the report's OWN table values for its table years.**

Naively pairing live PSD would have silently mismatched exactly the values the narrative recites.
Same class of trap as `ons_awe` / `ny_empire` vintage drift, but here it bites the *headline* number.

Column choice: prefer **New Post** (Post's own view, which is what the narrative argues); fall back
to *USDA Official* when New Post is a not-yet-published `0`. Ukraine `MY2026/27` sunflower area
shows why: Official `0`, New Post `5,450`. Fallbacks are counted in `run_report.official_fallbacks`.

## Forecast-not-measured (inherited from WASDE #41)

GAIN prose is **forecast-dominant**, not merely forecast-heavy — every Report Highlights and
Executive Summary leads with the coming marketing year (`UP2026-0011`: "forecast" ×15,
"projected" ×0). The deep-research pass that surfaced GAIN proposed *stripping* the forecast to stay
leakage-safe; that would **break** text↔series alignment rather than clean it, because the prose
describes the forecast year. Handled exactly as #41 does: the forecast year is the series'
**terminal point**, and the text is the *contemporaneous first-party forecast*, so there is no
future-value leakage. Recorded per record in `meta.forecast_caveat`.

## Two record shapes (per-post layout variance is the main engineering risk)

| shape | layout | example | records |
|---|---|---|---|
| `per_commodity` | prose **interleaved** after each commodity's table; bordered tables; **calendar**-year columns | livestock (`MX2026-0012`) | 1 per commodity (4) |
| `multi_commodity` | prose organized **by topic** (Production/Trade/Exports) discussing all commodities together; every PSD table grouped at the end under "PSD Data Statistics"; borderless tables; **marketing**-year columns (`2026/2027`) | oilseeds (`UP2026-0011`) | 1 per section, channels = union (18 ch) |

The second shape is a deliberate **anti-fake-scale** choice. Pairing that topical prose per
commodity would re-ship one paragraph under three labels — the boilerplate-reuse ban in
`SCHEMA.md`, and the same reason WASDE #41 refused to split sorghum/barley/oats out of its shared
`COARSE GRAINS` paragraph. It also means **records ≠ reports × commodities** for that family:
see "Scale" below.

## WASDE #41 de-duplication

The build was approved on condition of a dedup pass. Measured: **WASDE #41's six commodities are
all `U.S. ...` tables** and its world tables were deliberately not built, so foreign-post GAIN
series do not collide with it — overlap is near-zero *by construction*, not by filtering.
`config.dedup` keeps an explicit guard (US country × the six WASDE commodities) so a full run cannot
silently re-ship a #41 cell; hits are counted in `run_report.wasde_skipped`.

What is genuinely net-new: the **country-granular long tail** (full Mexican cattle balance sheet —
beg. stocks / dairy+beef cows / calf crop / cow+calf slaughter / loss and residual / ending
inventories; Ukrainian sunflowerseed × soybean × rapeseed) plus **country-specific narrative** that
has no WASDE analogue (New World Screwworm border closure, Bluetongue/EHD, Canadian canola
anti-dumping tariffs, refugee-driven demand).

## Alignment is auditable, not asserted

`meta.recite_evidence` records the channel, market year, value and **surface form** for every
recite, so a reviewer can grep the quoted form in the text:

```
animal_numbers_cattle_total_slaughter_1000_head  MY2025 = 6860  as '6.86'
   "...Slaughter is estimated to decline 3 percent in 2025 to 6.86 million heads..."
meat_beef_and_veal_production_1000_mt_cwe        MY2025 = 2170  as '2.17'
   "...beef production is estimated to decrease 4 percent in 2025 to 2.17 MMT CWE..."
```

The matcher is deliberately **strict**, because a loose one manufactures the fake alignment that got
openFDA/NHTSA/CFPB killed from this corpus:
* **exact** surface forms only, no rounding — `2,865` may not claim the prose's "2.9 MMT", and
  `1,259` may not claim "1.3 million";
* a **compatible unit word** must follow the number, so an MT channel cannot claim a "… ha" figure
  (an oilseed *crush* of 1,800 was being credited to a "1.8 million **ha**" *area* before this rule);
* numeric boundaries, so `2000` cannot match inside "20,000".

Consequence, stated honestly: reports that **round** in prose land as `describes`, not `recites`.
Ukraine writes "5.4 million ha" for a New Post `5,450` → no exact match → `describes`. A tolerant
tier would reclassify these but reinvites fake alignment; not worth it.

## Scale

FAS publishes **2,000–3,000 GAIN reports/yr, roughly half scheduled periodic** (the commodity
annual/semi-annual reports that carry PSD tables), across ~90 overseas posts. A single oilseeds
annual carries ~10 PSD balance sheets.

**Honest correction to the pre-build estimate:** density is **layout-dependent**, and the
`multi_commodity` family collapses toward 1–3 records/report (not 1/commodity) to avoid prose
reuse. So "reports × commodities" overstates the ceiling. `per_commodity` families (livestock and
similar) are the density carriers at ~4 records/report. >10k remains realistic across a 10–15-year
archive, but the mix of layout families — which the server must measure over the real archive —
sets the true number. Server must also confirm current-template depth (how far back the
`USDA Official / New Post` rendering is consistent).

## Known gaps / server to-dos

* **Report enumeration is unsolved.** The download API
  (`newgainapi/api/Report/DownloadReportByFileName?fileName=…`) works and is what this build uses,
  but no working *list* endpoint was found (`gain.fas.usda.gov` is a JS SPA; two candidate list
  paths 404). The demo pins filenames in config. The server needs a real enumerator (SPA XHR
  inspection, or the published GAIN report-schedule PDF) before a full run.
* **`CH2026-0032` (China oilseeds) has no extractable PSD table** — 0 pages matched either table
  strategy despite 50 pages of narrative. Some posts ship tables as images or in a third layout;
  such reports must be counted and skipped, not silently dropped.
* **Unmapped table labels** are reported in `run_report.unmapped_labels` rather than dropped
  silently. Currently unmapped and genuinely absent from PSD: `Area Planted` (oilseeds),
  `Other Slaughter`, `Total Dom. Consumption` vs PSD's `Domestic Consumption`. Extend
  `attribute_aliases` as coverage grows.
* **Prose anchors are per-family config** (`prose_heading`, or prose-after-table). A 2–3k
  reports/yr run needs a generalized section locator; layout variance across ~90 posts is the
  single biggest unknown in this package.
* `datasets/README.md` still needs a row for this package (shared file — flagged separately, not
  edited here).

## Provenance

* Reports: `https://apps.fas.usda.gov/newgainapi/api/Report/DownloadReportByFileName?fileName=<file>`
  (the `www.fas.usda.gov/data/gain-report/...` direct PDF path **403s**).
* Series: `https://apps.fas.usda.gov/psdonline/downloads/psd_{livestock,oilseeds,grains_pulses,cotton,sugar,coffee,dairy}_csv.zip`
  — keyless bulk download, `Country_Name × Commodity_Description × Attribute_Description ×
  Market_Year → Value` + `Unit_Description`.
* Demo reports: `MX2026-0012` (Livestock and Products Semi-annual, Mexico City, 2026-02-26) and
  `UP2026-0011` (Oilseeds and Products Annual, Kyiv, 2026-04).
