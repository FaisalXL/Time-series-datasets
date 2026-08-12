# ESPN US Majors (NBA + NFL + NHL) Recaps + Period Score Series → CPT

> **Status: Built (demo).** One record = **one finished game** — the real AP wire recap (served via ESPN's API) paired with that game's **period-by-period running score** (away/home cumulative). `text_quality: "real"` always; games with no usable recap are dropped. Demo build = 50 records (17 NBA / 17 NFL / 16 NHL). Verified scale for the full universe (2014-15 season → present, all 3 leagues): **~29,000**.
>
> **⚠️ License:** the recap prose is **Associated Press wire copy** — `article.source == "AP"` on every game checked across all 3 leagues — served through ESPN's API. This is a **different, likely stricter** copyright chain than [`45_cricket_report_overseries`](../45_cricket_report_overseries/) (ESPNcricinfo's own staff journalists). The committed `output/` + `samples/` are a **capped 50-record demo for the lead to inspect** (internal review, **not** distribution). Do **not** scale past the demo or publish until redistribution is cleared with Charon — same open question as Cricket, now applying to this and 3 more sports sources (see `../../docs/scouting_build_queue.md`).

**What it is:** the AP recap narrates the game's *shape* — a comeback, a blowout, a lead held throughout — which is exactly what the period-by-period score channels encode. That makes this a genuine **"describes"** pairing: an independent news account lined up against the numbers it's narrating, not just co-located with them.

**Retrieval reuses Cricket's already-solved plumbing.** Same ESPN parent-host API (`site.api.espn.com`) that bypasses the `www.espncricinfo.com`-style bot-blocks; same `article.story` field for the recap text. No bulk archive exists for this source (unlike Cricsheet for cricket), so event discovery walks a date range via the scoreboard endpoint per league instead of unzipping one file.

---

#### 📄 Text — AP recap via ESPN's API

| | |
|---|---|
| **What** | The game recap (~300–500 words): an AP wire story. `text` = the cleaned recap with `<ts></ts>` appended directly — no generated/templated framing sentence. |
| **Source** | `site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary?event={event_id}` → JSON `article.story` (HTML, stripped), plus `headline` / `source` / `published`. |
| **`text_source`** | `"third_party"` — this is independent AP journalism about the game, not an official team/league statement (same reasoning that would apply to Cricket's ESPNcricinfo bylines, had it been tagged post-freeze). |
| **`text_quality`** | `"real"`. Games with no usable recap are **dropped**, never synthesized. |
| **Coverage** | 2/52 discovered games in the demo window had no recap (`no_report`) — high coverage, similar to Cricket's ~74% for a much older/thinner archive. |

#### 📈 Time series — period-by-period running score

| | |
|---|---|
| **What** | 2 channels, one step per period of play (quarters for NBA/NFL, periods for NHL; OT periods included if the game went there) |
| **Source** | ESPN's own play-by-play, extracted from the same `summary` response as the recap — no second fetch. NBA/NHL expose a flat `plays[]`; **NFL nests plays under `drives.previous[].plays[]`** (a real structural difference between sports, handled by `_flatten_plays()`). |
| **Cadence** | `1prd` — a new domain-native epoch (added to `FREQ_RE` in `../schema/validate.py`, same process Cricket used to add `1over`). One step = one period of play, not wall-clock time. |

| Channel (`unit`) | Meaning |
|---|---|
| `away_score_cumulative` | Away team's running score at the end of each period |
| `home_score_cumulative` | Home team's running score at the end of each period |

**Extraction is a running max, not "the last play's score per period"** — see Key Issues below for why that distinction is load-bearing, not stylistic.

**Record shape** (real — Nuggets @ Cavaliers, Jan 3 2026, event `401810333`):
```json
{
  "text": "CLEVELAND -- Donovan Mitchell scored 33 points... the Cavaliers beat the short-handed Denver Nuggets 113-108... who trailed 105-101 with 4:43 remaining before scoring 10 consecutive points...\n\n<ts></ts>",
  "timeseries": [
    {"values": [24, 59, 97, 108], "unit": "away_score_cumulative", "freq": "1prd"},
    {"values": [28, 62, 88, 113], "unit": "home_score_cumulative", "freq": "1prd"}
  ],
  "task_type": "world_knowledge", "text_quality": "real",
  "event_id": "401810333", "league": "NBA",
  "away_team": "Denver Nuggets", "home_team": "Cleveland Cavaliers",
  "game_date": "2026-01-03T00:30Z", "n_periods": 4,
  "final_away_score": 108, "final_home_score": 113,
  "report_url": "https://www.espn.com/basketball/game/_/gameId/401810333",
  "report_headline": "Donovan Mitchell scores 33 points, Cavaliers outlast short-handed Nuggets 113-108",
  "report_source": "AP", "report_published": "2026-01-03T03:15:59Z",
  "dataset": "espn_us_majors", "source": "https://www.espn.com/basketball/game/_/gameId/401810333",
  "series_id": "espn_nba_401810333",
  "license": "proprietary-review", "text_source": "third_party", "alignment": "describes",
  "domain": "sports", "region": "US", "period_start": "2026-01-03", "period_end": "2026-01-03"
}
```

**The alignment is exact, including shape, not just the final score:** the recap says the Nuggets *"trailed 105-101 with 4:43 remaining before scoring 10 consecutive points"* — Q4 shows the Cavaliers (home) outscoring Denver 25–11 (113−88=25 vs 108−97=11), the exact swing the text narrates. Headline *"113-108"* = `final_home_score`/`final_away_score` exactly.

---

## Key issues

- **No generated text.** An earlier version of this build appended a templated closing sentence to introduce the period-by-period score series before `<ts></ts>`. That sentence was not from the AP recap — it was synthesized by the build script. Fixed 2026-07-24: `<ts></ts>` is now appended directly to the real recap text with nothing generated in between.
- **⚠️ License is the one open decision (for Charon, not self-cleared).** Recap text is AP wire copy — a *different* copyright holder than Cricket's ESPNcricinfo staff writers, and confirmed **stricter** in kind (wire services license redistribution far more tightly than most in-house sports journalism). The committed `output/`+`samples/` are a **50-record demo for internal inspection only**. This is the same open question already gating Cricket's ~44k records — see `../../docs/scouting_build_queue.md` for the combined ~157k-record scope across all 5 sports sources.
- **Two real extraction bugs found and fixed during the build (not caveats to just document — actually fixed):**
  1. **ESPN's own play-by-play trails the true final scoring play with stale-score administrative events.** The literal last play in a period is often "End of the Nth Quarter" / "End of Game" — but these can carry a score snapshot **one score behind** the real final (observed live: last scoring play showed 113, the subsequent "End of Game" marker still read 112). Fix: `period_scores()` takes a **running max** per period, not the temporally-last play's value, since score is monotonically non-decreasing within a game.
  2. **NHL shootouts.** The shootout-deciding goal is **not** reflected in the play-by-play's `awayScore`/`homeScore` fields — those stay frozen at the tied regulation/OT score through the entire shootout period, while the *official* boxscore score (and the recap's own headline) awards the winner a +1. A naive extraction would silently ship a series that disagrees with the very text it's paired with. Fix: every record's extracted final score is cross-checked against the header's official boxscore score (`official_scores()`); any mismatch is dropped (`score_mismatch`), the same "don't ship a bad pairing" principle as Cricket dropping short/rain-affected innings. Verified: 0/50 headline-vs-series mismatches after the fix (was 1/50 before, from a shootout game).
- **NFL's play-by-play is structurally nested differently than NBA/NHL** — `drives.previous[].plays[]`, not a flat `plays[]`. Handled by `_flatten_plays()`; worth knowing before extending to other sports (e.g. MLB, which is at-bat/half-inning structured, not drive-structured — see the separate `mlb_statsapi` scouting entry).
- **`freq: 1prd`** is a new domain-native epoch (period/quarter of play, not wall-clock) — added to `FREQ_RE` in `../schema/validate.py`, same process the Cricket package used to add `1over`. Deliberately unified across NBA/NFL quarters and NHL periods rather than minting two separate tokens, since both represent "the game's natural broadcast segmentation."
- **Discovery has no bulk archive to lean on** (unlike Cricsheet for cricket) — event IDs are found by walking the scoreboard endpoint date-by-date. The demo window (`2026-01-01`–`2026-03-20`) was chosen specifically to span all 3 leagues' seasons (NFL playoffs run into January; NBA/NHL are mid-season through March). **Scaling to the full ~29k-record universe means walking every date back to the 2014-15 season across all 3 leagues** — a much longer discovery phase, but the same per-event logic.
- **Etiquette:** every fetch (scoreboard + summary) is rate-limited (`request_delay_s`, ~2–3 req/s) and cached per date/event under `.cache/`, so reruns don't re-hit ESPN.
- **Environment:** stdlib only for the core; PyYAML only to read config. Works on Python 3.9+.

## Scaling up — done, 2026-08-12

**Built: 33,266 records, 33,266/33,266 `validate.py --strict`, 0 warnings.** The bank lives in
corpus storage, not here: `/data/defu/cpt_corpus/packages/51_espn_us_majors/output.jsonl`
(234 MB). This directory keeps the builder and the 50-record demo.

The ~29,000 ceiling this section used to aim at was **17% too low**. Measured instead of
projected: **36,659 finished games** counted across NBA+NFL+NHL for 2014-07 → 2026-08
(NBA 15,699 / NFL 3,939 / NHL 17,021), of which **92.7%** carry a ≥400-char recap.

Use the sanctioned runner for a rebuild — it redirects output into the corpus tree, then gates
and records it:

```bash
python3.11 /data/defu/cpt_corpus/run_full.py 51_espn_us_majors
```

Two things that made the full run practical:

1. **`discovery.event_ids_file`** — a pre-built event list. Discovery drops from **3.4 h to
   0.2 s**, because the day-by-day scoreboard walk is 4,425 days × 3 leagues = 13,275 requests
   and all of it happens before a single record is emitted. Season-aware per-league windows
   (the old suggestion below) would have trimmed that; skipping the walk removes it.
2. **`&limit=1000` on any range query.** Without it the scoreboard endpoint silently truncates
   at 100 events and still returns HTTP 200 — NBA January 2024 returns 100 of 233 real games.
   A 57% undercount that looks like a successful call.

Still open: `min_periods` per league if extending to sports with other OT/shootout conventions
(NHL's regular shootout case is already handled via `score_mismatch`). And **the licence
question above is unchanged** — 33,101 of the 33,266 records are AP wire copy, so nothing here
may be distributed until that is cleared.

## Run

```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=5   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50, round-robin across leagues)

# full scale -> corpus storage, gated + recorded (NOT into this repo)
python3.11 /data/defu/cpt_corpus/run_full.py 51_espn_us_majors
```

> ⚠️ `--set` cannot set the discovery dates: the shared `coerce()` helper turns `20141201`
> into an int because it matches `^-?\d+$`, and `date_range()` calls `strptime` on it, so
> `--set data.discovery.start_date=...` dies with `TypeError`. Edit a config copy instead.

Fetches real AP recaps via ESPN's API — mind the license note before scaling past the demo or sharing output.

**Output:** `output/espn_us_majors_cpt.jsonl` + `output/run_report.json`; `samples/example_output.jsonl` = first 3 records. Committed as a **50-record demo for review** (not a full build). `.cache/` (git-ignored) holds per-date scoreboard responses and per-event summaries.

**Sources:** [ESPN parent sports API](https://site.api.espn.com/) `site.api.espn.com` (same host Cricket already uses) — recap text is Associated Press wire copy (**copyrighted — redistribution pending Charon's sign-off**); play-by-play/boxscore data is ESPN's own.
