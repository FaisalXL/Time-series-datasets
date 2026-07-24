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
| **What** | The game recap (~300–500 words): an AP wire story. `text` = the cleaned recap + one framing sentence *"Score progression by period, away then home ({away} at {home}): `<ts></ts>`"*. |
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
  "text": "CLEVELAND -- Donovan Mitchell scored 33 points... the Cavaliers beat the short-handed Denver Nuggets 113-108... who trailed 105-101 with 4:43 remaining before scoring 10 consecutive points...\n\nScore progression by period, away then home (Denver Nuggets at Cleveland Cavaliers): <ts></ts>",
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

- **⚠️ License is the one open decision (for Charon, not self-cleared).** Recap text is AP wire copy — a *different* copyright holder than Cricket's ESPNcricinfo staff writers, and confirmed **stricter** in kind (wire services license redistribution far more tightly than most in-house sports journalism). The committed `output/`+`samples/` are a **50-record demo for internal inspection only**. This is the same open question already gating Cricket's ~44k records — see `../../docs/scouting_build_queue.md` for the combined ~157k-record scope across all 5 sports sources.
- **Two real extraction bugs found and fixed during the build (not caveats to just document — actually fixed):**
  1. **ESPN's own play-by-play trails the true final scoring play with stale-score administrative events.** The literal last play in a period is often "End of the Nth Quarter" / "End of Game" — but these can carry a score snapshot **one score behind** the real final (observed live: last scoring play showed 113, the subsequent "End of Game" marker still read 112). Fix: `period_scores()` takes a **running max** per period, not the temporally-last play's value, since score is monotonically non-decreasing within a game.
  2. **NHL shootouts.** The shootout-deciding goal is **not** reflected in the play-by-play's `awayScore`/`homeScore` fields — those stay frozen at the tied regulation/OT score through the entire shootout period, while the *official* boxscore score (and the recap's own headline) awards the winner a +1. A naive extraction would silently ship a series that disagrees with the very text it's paired with. Fix: every record's extracted final score is cross-checked against the header's official boxscore score (`official_scores()`); any mismatch is dropped (`score_mismatch`), the same "don't ship a bad pairing" principle as Cricket dropping short/rain-affected innings. Verified: 0/50 headline-vs-series mismatches after the fix (was 1/50 before, from a shootout game).
- **NFL's play-by-play is structurally nested differently than NBA/NHL** — `drives.previous[].plays[]`, not a flat `plays[]`. Handled by `_flatten_plays()`; worth knowing before extending to other sports (e.g. MLB, which is at-bat/half-inning structured, not drive-structured — see the separate `mlb_statsapi` scouting entry).
- **`freq: 1prd`** is a new domain-native epoch (period/quarter of play, not wall-clock) — added to `FREQ_RE` in `../schema/validate.py`, same process the Cricket package used to add `1over`. Deliberately unified across NBA/NFL quarters and NHL periods rather than minting two separate tokens, since both represent "the game's natural broadcast segmentation."
- **Discovery has no bulk archive to lean on** (unlike Cricsheet for cricket) — event IDs are found by walking the scoreboard endpoint date-by-date. The demo window (`2026-01-01`–`2026-03-20`) was chosen specifically to span all 3 leagues' seasons (NFL playoffs run into January; NBA/NHL are mid-season through March). **Scaling to the full ~29k-record universe means walking every date back to the 2014-15 season across all 3 leagues** — a much longer discovery phase, but the same per-event logic.
- **Etiquette:** every fetch (scoreboard + summary) is rate-limited (`request_delay_s`, ~2–3 req/s) and cached per date/event under `.cache/`, so reruns don't re-hit ESPN.
- **Environment:** stdlib only for the core; PyYAML only to read config. Works on Python 3.9+.

## Scaling up

The demo's 50-record, 3-league, 3-month window is deliberately narrow. To move toward the verified ~29,000-record ceiling:
1. Widen `data.discovery.start_date`/`end_date` per league to that league's actual season windows back to 2014-15 (NBA/NHL: Oct–Jun; NFL: Sept–Feb) — the current code accepts one shared window for simplicity, but a full build should probably run leagues on separate, season-aware windows rather than one continuous multi-year walk (fewer wasted off-season scoreboard calls).
2. Raise `output.max_records` — but **do not commit or distribute** a full-scale build until the license question above is resolved.
3. Consider `min_periods` per league if extending to sports with different OT/shootout conventions (already handled for NHL's regular shootout case via `score_mismatch`).

## Run

```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=5   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50, round-robin across leagues)
```
Fetches real AP recaps via ESPN's API — mind the license note before scaling past the demo or sharing output.

**Output:** `output/espn_us_majors_cpt.jsonl` + `output/run_report.json`; `samples/example_output.jsonl` = first 3 records. Committed as a **50-record demo for review** (not a full build). `.cache/` (git-ignored) holds per-date scoreboard responses and per-event summaries.

**Sources:** [ESPN parent sports API](https://site.api.espn.com/) `site.api.espn.com` (same host Cricket already uses) — recap text is Associated Press wire copy (**copyrighted — redistribution pending Charon's sign-off**); play-by-play/boxscore data is ESPN's own.
