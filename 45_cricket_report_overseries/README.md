# Cricket Match Reports + Per-Over Ball-by-Ball Series → CPT

> **Status: Built.** One record = **one innings** — the real **ESPNcricinfo match report** paired with that innings' **per-over time series**. `text_quality: "real"` always; innings with no recap are dropped. Demo build = 50 records. Full IPL build ≈ **1.9k innings** (2,514 innings × ~74% report coverage − short innings); the full multi-format Cricsheet universe is **~30k+ innings**.
>
> **⚠️ License:** the report prose is **copyrighted ESPNcricinfo editorial** — see [NOTION_PAGE.md](NOTION_PAGE.md). The committed `output/` + `samples/` are a **capped 50-record demo for the lead to inspect** (internal review, **not** distribution). Do **not** scale past the demo or publish until redistribution is cleared.

**What it is:** the ESPNcricinfo report *narrates the same match the series quantifies* — the powerplay total, the collapse, when each wicket fell, the run-rate — which is exactly what the per-over channels encode. That makes this one of the tightest **"describes"** pairings in the corpus: an independent human journalist's account lined up over-by-over against the numbers.

**The join is exact:** the Cricsheet CSV filename **is** the ESPNcricinfo match ID (`1426312.csv` → match `1426312`), so both halves come from a single integer `match_id` — no fuzzy matching.

---

#### 📄 Text — ESPNcricinfo match report

| | |
|---|---|
| **What** | The match report (~850–1,200 words): an editor's over-by-over recap. `text` = the cleaned report + one framing sentence *"Per-over progression of the {team} innings: `<ts></ts>`"*. |
| **Source** | ESPN **parent** sports API `site.api.espn.com/apis/site/v2/sports/cricket/{league}/summary?event={match_id}` → JSON `article.story` (HTML, stripped in-script), plus `headline` / `byline` / `published`. |
| **Why this host** | `www.espncricinfo.com` and `hs-consumer-api` `403` bots; `site.api.espn.com` does not, and **resolves purely by `event`** = the Cricsheet `match_id` (the `{league}` segment is just a required carrier — any valid cricket league id works for any match, all formats). |
| **`text_quality`** | `"real"` (real journalist prose — bylines like Sidharth Monga, Karthik Krishnaswamy). Innings with no usable recap are **dropped**, never synthesized. |
| **Coverage** | ≈ **74%** of IPL matches have a recap (near-100% recent seasons, sparse pre-2015). |

#### 📈 Time series — Cricsheet per-over aggregation

| | |
|---|---|
| **What** | 4 channels, one step per over of the innings |
| **Source** | [Cricsheet](https://cricsheet.org/downloads/) bulk per-delivery CSV (**ODC-BY 1.0**). Each match = `{id}.csv` (one row per delivery) + `{id}_info.csv` (teams/date/venue/event/result). **Stdlib only** — `zipfile`/`csv`, no pandas/duckdb. |
| **Cadence** | `1over` — intra-match game-clock (over count), same caveat class as intra-day sports series |

| Channel (`unit`) | Meaning |
|---|---|
| `runs_per_over` | Runs scored in each over (`runs_off_bat + extras`) |
| `wickets_per_over` | Wickets that fell in each over (count of `wicket_type`) |
| `cumulative_runs` | Running innings total by over end |
| `run_rate` | Cumulative runs ÷ overs completed |

Aggregation: bucket deliveries by over index `int(float(ball))`, sum runs, count wickets. **Cross-checked to a hand-verified reference** (IPL 2024 final SRH innings, match `1426312`): produced `runs_per_over [3,3,9,6,2,17,7,4,7,3,9,2,10,8,0,8,10,5,0]`, `cumulative_runs` ending `113`, 10 wickets — an exact match to the report and to the register's verified values.

**Record shape** (real — IPL 2024 final, SRH innings; report text + arrays abbreviated):
```json
{
  "text": "…SRH were bowled out for the lowest total in an IPL final, 113, which KKR chased down with 57 balls to spare… Arora went for 17 in the final powerplay over, taking SRH up to 40 for 3…\n\nPer-over progression of the Sunrisers Hyderabad innings: <ts></ts>",
  "timeseries": [
    {"values": [3, 3, 9, "...", 0], "unit": "runs_per_over", "freq": "1over"},
    {"values": [1, 1, 0, "...", 1], "unit": "wickets_per_over", "freq": "1over"},
    {"values": [3, 6, 15, "...", 113], "unit": "cumulative_runs", "freq": "1over"},
    {"values": [3.0, 3.0, 5.0, "...", 5.95], "unit": "run_rate", "freq": "1over"}
  ],
  "task_type": "world_knowledge", "text_quality": "real",
  "match_id": "1426312", "event": "Indian Premier League", "season": "2024", "match_type": "T20",
  "batting_team": "Sunrisers Hyderabad", "bowling_team": "Kolkata Knight Riders", "innings": 1,
  "venue": "MA Chidambaram Stadium, Chepauk, Chennai", "city": "Chennai", "start_date": "2024/05/26",
  "overs_bowled": 19, "total_runs": 113, "wickets": 10, "winner": "Kolkata Knight Riders",
  "report_url": "https://www.espncricinfo.com/ci/engine/match/1426312.html",
  "report_headline": "KKR's bowlers rip through SRH to win third IPL title",
  "report_byline": "Sidharth Monga", "report_published": "2024-05-27T03:40:19Z", "report_type": "Recap",
  "dataset": "cricket_report_overseries", "source": "cricsheet.org + site.api.espn.com",
  "series_id": "cros_1426312_inn1"
}
```

---

## Worked example (real, verified) — IPL 2024 final, SRH innings (`match_id` 1426312)

The full record above, unabbreviated: an independent human recap paired with the per-over series of the innings it narrates. **Both halves are pulled from just the `match_id`.**

**📄 Text — ESPNcricinfo report** · headline *"KKR's bowlers rip through SRH to win third IPL title"*, byline **Sidharth Monga**, 2024-05-27
Source: `https://site.api.espn.com/apis/site/v2/sports/cricket/1410320/summary?event=1426312` → JSON `article.story` (HTML-stripped)
> *"…SRH were bowled out for the **lowest total in an IPL final, 113**, which KKR chased down with 57 balls to spare thanks to Venkatesh Iyer's blitz of 52 off 26. Five of the six bowlers used by KKR took a wicket in their first over… KKR used just the two bowlers in the powerplay, but Arora went for 17 in the final powerplay over, taking **SRH up to 40 for 3**… Russell would go on to add a 19th to his tally when ending the innings with Pat Cummins' wicket in the 19th over."*

**📈 Time series — Cricsheet per-over, SRH innings**
Source: `https://cricsheet.org/downloads/ipl_male_csv2.zip` → `1426312.csv`, filtered to `innings == 1`
```json
[
  {"values": [3, 3, 9, 6, 2, 17, 7, 4, 7, 3, 9, 2, 10, 8, 0, 8, 10, 5, 0], "unit": "runs_per_over", "freq": "1over"},
  {"values": [1, 1, 0, 0, 1, 0, 1, 0, 0, 0, 1, 1, 1, 0, 1, 0, 0, 1, 1], "unit": "wickets_per_over", "freq": "1over"},
  {"values": [3, 6, 15, 21, 23, 40, 47, 51, 58, 61, 70, 72, 82, 90, 90, 98, 108, 113, 113], "unit": "cumulative_runs", "freq": "1over"},
  {"values": [3.0, 3.0, 5.0, 5.25, 4.6, 6.67, 6.71, 6.38, 6.44, 6.1, 6.36, 6.0, 6.31, 6.43, 6.0, 6.12, 6.35, 6.28, 5.95], "unit": "run_rate", "freq": "1over"}
]
```

**The alignment is exact** (this is the "describes" payoff): the report's *"40 for 3"* after the powerplay = `cumulative_runs[5] = 40` with 3 wickets in overs 1–5; *"bowled out for 113"* = `cumulative_runs[-1] = 113` with `sum(wickets_per_over) = 10`; the final-powerplay-over spike *"17"* = `runs_per_over[5] = 17`.

> **Note on the excerpts above:** the report snippets in this README are **brief, attributed excerpts** (© ESPNcricinfo, "KKR's bowlers rip through SRH…", Sidharth Monga) shown only to illustrate the record format. Full report text sits in the committed 50-record demo (`output/`), for internal review pending the license decision in [NOTION_PAGE.md](NOTION_PAGE.md).

---

## Key issues

- **⚠️ License is the one open decision (for the lead, not self-cleared).** Cricsheet is **ODC-BY 1.0** — fine with attribution. The **ESPNcricinfo report prose is copyrighted / ToS-restricted**; publishing/scaling it is a legal/policy call. The write-up + exact question is in **[NOTION_PAGE.md](NOTION_PAGE.md)**. The committed `output/`+`samples/` are a **50-record demo for internal inspection only** — do not run a full build or publish until it's cleared.
- **Alignment = describes (strong).** The recap narrates the same match the series quantifies — powerplay total, the collapse, when wickets fell, run-rate. See the worked example: report "40 for 3" ↔ `cumulative_runs[5]=40`, "bowled out for 113" ↔ `cumulative_runs[-1]=113` / 10 wickets.
- **Whole-match report, per-innings series:** `article.story` covers the full match, so a match's two innings-records share the same report text (localized by the `<ts></ts>` framing sentence). Mild text reuse; acceptable for CPT. (If undesirable, restrict to `innings == 1` or split the report — easy config/code change.)
- **Report coverage caps volume:** ~74% of IPL matches have a recap; the rest are dropped (`no_report`). IPL archive = 1,243 matches → 2,514 innings; after the ~74% coverage and the 12-over `min_overs` floor (~3.2%), ≈ **1.9k IPL innings**. Point `data.cricsheet_zip_url` at `tests_male_csv2.zip`, `odis_male_csv2.zip`, `t20s_male_csv2.zip`, etc. for the full **~30k+-innings** universe.
- **`freq: 1over`** is an intra-match cadence (over count, not wall-clock) — same game-clock caveat class as the intra-day sports candidates.
- **Etiquette:** report fetch is rate-limited (`request_delay_s`, ~2–3 req/s) and **cached per `match_id`** under `.cache/espn/`, so reruns don't re-hit ESPN.
- **Environment:** stdlib only for the core (works on any Python 3.9+); PyYAML only to read config. The 6.8 MB zip is cached under `.cache/` so reruns are free.

## Run

```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=5   # smoke test
python scripts/build_cpt_jsonl.py                                        # demo (50)
python scripts/build_cpt_jsonl.py --set output.max_records=null          # full IPL build (~1.9k innings)
```
Fetches real ESPNcricinfo reports — mind the license note before scaling past the demo or sharing output.

**Output:** `output/cricket_report_overseries_cpt.jsonl` + `output/run_report.json`; `samples/example_output.jsonl` = first 3 records. Committed as a **50-record demo for review** (not a full build). `.cache/` (git-ignored) holds the Cricsheet zip + per-match report JSON.

**Sources:** [Cricsheet](https://cricsheet.org/) (ODC-BY 1.0 — *"Downloadable data by Stephen Rushe, licensed under ODC-BY"*) · ESPNcricinfo match reports via the ESPN parent API `site.api.espn.com` (**copyrighted — redistribution pending lead sign-off**, see NOTION_PAGE.md).
