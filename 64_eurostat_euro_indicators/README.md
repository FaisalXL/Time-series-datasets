# Eurostat "Euro indicators" Releases + Eurostat API → CPT

> **Status: Demo built 2026-08-12 — 4 records, 6 indicator families, 22 verified channels.**
> `schema/validate.py --strict` clean (4/4, 0 errors / 0 warnings).
> All 11 evidence matches exact (0.00% relative error), 0 out of tolerance.
> Scout-lane demo. The server agent runs the full federation.

**What it is:** one record = **one euro-indicator news release** — its own **verbatim** headline
prose paired with the trailing **36-month multi-channel window** of the exact Eurostat dataset
the release reports, with the pairing proved **figure by figure**.

```
"Eurostat estimates that 13.317 million persons in the EU, of whom 11.130 million in the euro
 area, were unemployed in June 2026. ... In June 2026, the youth unemployment rate was 15.5%
 in the EU ... and 14.8% in the euro area."
                          ↕  evidence-checked, per claim
 eu_unemployed_thousands  2026-06 = 13317      ea_unemployed_thousands  2026-06 = 11130
 eu_youth_unemployment_rate_pct   = 15.5       ea_youth_unemployment_rate_pct   = 14.8
```

| | |
|---|---|
| **Domain / region** | macro / `EU` |
| **License** | **CC BY 4.0**, commercial reuse explicitly authorised (verbatim in `config.example.yaml`) |
| **Alignment** | `recites` 4/4 — assigned by evidence, never assumed |
| **Freq / depth** | `1M`, 36-month window, `min_points` 32 |
| **Text** | median 1,770 chars (~440 tokens), max 2,000 (the 500-token cap), **100% verbatim** |
| **Access** | keyless API, **but** see the two access traps below |

## Scale

Live server-side count on the listing: **4,442 releases** (2026-08-11). DR#6 read 4,291, so the
family grows roughly monthly. One release = one record, so ~4,400 is also the record ceiling —
**there is no per-country multiplier** (see "no country expansion" below).

## Three traps this package exists to handle

### 1. The EU Login wall
`ec.europa.eu/eurostat/web/products-euro-indicators` — the URL the source docs give —
**301-redirects to ECAS / EU Login**. The working listing is `/web/main/news/euro-indicators`.
The fetcher raises if a response looks like the login page rather than caching it as content.

### 2. The silent-empty geo trap — the important one
Filtering on a geo code a dataset **does not carry** returns **HTTP 200 with a well-formed
JSON-stat envelope whose `value` object is empty**. Not a 404, not an error — a silent zero.
DR#6's own recipe used `geo=EA20` and got exactly this.

Euro-area codes **differ per dataset**, measured the same day on the same API:

| dataset | euro-area codes present |
|---|---|
| `une_rt_m` | `EA21` only |
| `prc_hicp_manr` | `EA`, `EA20`, `EA19` — **no `EA21` at all** |
| `sts_inpr_m` | `EA21`, `EA20`, `EA19` |

So **no single hardcoded code works**. The build reads each dataset's own `geo` dimension and
resolves against a preference list, then **raises** if a filter yields zero values. The demo
resolves `EA21` for unemployment and `EA20` for inflation — automatically, in the same run.

This guard earns its keep beyond geo: wiring `services_production` with `nace_r2: G-N_STS`
(a real code in that dataset's dimension list) returned 200 + empty. The guard caught it; the
working combination is `G-N_X_K` with `s_adj: SCA`. Without the guard that family would have
shipped as an empty channel.

### 3. Tables masquerading as prose
A naive `<p>` scrape returns ~30,000 chars per release. Strip `<table>` first and the real prose
is **~4,100 chars** — **84% of the naive extraction is table content**. Everything downstream
(figure density, text cap, dedup) is wrong if this is skipped.

## Evidence guards — why `ev` counts are lower than they could be

The first build produced an unemployment record with **`ev=8`, and several were false**. The
release discusses far more series than any channel set holds, so *"the unemployment rate for
**women** was 6.4%"* was credited to the euro-area **total** rate (really 6.3) because 6.4 was a
real value earlier in the same window. Schema validation passed it 8/8.

| guard | effect |
|---|---|
| `proximity_chars: 140` | figure must sit near that channel's own keywords |
| `disqualifying_terms` | a modifier in the figure's clause (`women`, `men`, `young`, `core`, `excluding`) that is not part of the channel's identity leaves the figure **unattributed** rather than mis-credited. Word-boundary matched, so `men` cannot fire inside `women`. |
| `signed: true` + signed parsing | `−0.9%` energy inflation no longer matches a stored `+0.9`; the minus sign and en-dash are parsed, and direction words are cross-checked |

Result: unemployment `ev` fell 8 → 5, and all 5 survivors are exact. **Prefer 5 true matches to
8 that include false ones** — a polluted evidence array inflates apparent quality and is
invisible to `validate.py`.

The guard is deliberately conservative: *"the unemployment rate for women was 6.1% ... and the
unemployment rate for men was 5.9%"* is one sentence, so `women` sits in the men-figure's clause
and the men channel goes unattributed. Silence is cheap; a wrong attribution is not.

## No country expansion

The per-country numbers in a euro-indicator release live in **tables**, not prose. With tables
stripped, the only country mentions in prose are boilerplate membership lists (*"Euro area
(EA21): Belgium, Bulgaria, …"*), which this build drops. So a per-member-state record would be
a table row with no narrating prose — it would fail the alignment bar. **~4,400 is the ceiling,
not ~4,400 × 27.**

## Known limitation — reference-period resolution

`reference_period()` takes the latest *"Month YYYY"* named in the prose. For the unemployment
release that is correct (2026-06). For the retail-trade and producer-price releases the demo
resolved 2026-07 while the headline claim ("down by 0.3%") concerns an earlier month, so the
window ends one month late and the **headline** figure is not the one verified — the matched
evidence is still real and correctly attributed, just not the lead claim. Tighten this before a
full run; it is the same class of bug as FHFA #59's URL-month-vs-index-month offset.

## Federation is config-only

`retail_trade` and `services_production` were added **after** the pipeline was written, as a
test of that claim: two config blocks, no build-script change. Both worked (`services` needed
one code correction, caught by the silent-empty guard). Wiring the remaining families visible on
listing page 1 — GDP, government debt/deficit, construction, household income — is the same
exercise; `no_family` was 5/11 in the demo purely because they are not wired yet.

## Throttling

Eurostat throttles hard on concurrency — bulk pulls measured **3/150** and **19/80** success at
6–8 threads. This build is **strictly serial** with `min_interval_s: 1.5`. Do not parallelise it.

## Freq token

`1M` is one **month**; `1m` is one **minute**. (#41 and #59 label monthly series `1m` and pass
validation silently.)

## Run it

```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run
python scripts/build_cpt_jsonl.py                                       # demo
python scripts/build_cpt_jsonl.py --set output.max_records=null         # full build
python ../schema/validate.py output/eurostat_euro_indicators_cpt.jsonl --strict
```

**Attribution required by CC BY 4.0:** *"Source: Eurostat"* — carried on every record at
`meta.attribution`.
