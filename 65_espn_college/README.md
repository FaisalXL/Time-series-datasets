# ESPN College Sports Recaps + Score-Progression Series → CPT

> **Status: demo built 2026-08-12 — 50 records across 3 leagues, `validate.py --strict` clean
> (50/50 and 5/5 samples, 0 errors / 0 warnings).**
> **Licence-gated: every record is `proprietary-review` (AP wire copy). Not cleared for
> distribution — this is the same rightsholder ask as `51_espn_us_majors`.**

**What it is:** one record = **one finished college game** — the real wire-service recap (served
via ESPN's API) paired with that game's **play-by-play running score** (away/home cumulative, one
point per play). The recap *describes* the shape of the game the series quantifies →
`alignment: describes`.

Sibling of `51_espn_us_majors` (NBA/NFL/NHL). Same endpoints, same play-extraction logic, same
licence question, different universe: **FBS football + Division I men's and women's basketball**.

| | |
|---|---|
| **Demo records** | **50** (CFB 10 · MCB 20 · WCB 20), all `article.source == "AP"` |
| **Domain / region** | sports / `US` |
| **License** | ⚠️ `proprietary-review` — AP wire copy via ESPN's API. **Same rightsholder as `51`** |
| **Alignment** | `describes` · `text_quality: real` (verbatim recap; nothing generated, nothing templated) |
| **Series** | 2 channels (away/home cumulative score), `freq: 1play`, demo median **326 plays** |
| **Text** | verbatim recap prose, demo median 1,824 chars |
| **Built via** | `schema/emit.py` — records are born strict-clean (unlike `51`, whose committed samples are pretty-printed JSON and return 0/2868 under `--strict`) |

---

## Why this is worth having despite the licence gate

**It costs almost nothing and it rides on a licence ask that is already being made.** `51`'s
recaps are AP; so are these. Scoping the request to **AP** covers `51` (33,960 measured records)
*and* this package, from one rightsholder. Conversely, if AP declines, both are zero — so
**do not spend harvest time here before the AP answer**.

`45_cricket_report_overseries` is a **separate and materially easier** ask (ESPNcricinfo in-house
staff journalism, single rightsholder). Do not bundle the two requests.

---

## Measured 2026-08-12 — every number below was probed live, none is scouted

The tier's scouting note carried "ESPN college ~15k" with no package behind it. What is actually
there:

| league | in-season volume | recap ≥400 chars | rightsholder (of covered) | series depth |
|---|--:|--:|---|--:|
| **CFB** (FBS football) | 960 finished games in calendar 2024 | **82.5%** | **AP 33/33** | 155–207 plays |
| **MCB** (D-I men's basketball) | **~64 D-I games/day** (11 sampled days, range 10–155) | **92.5%** | see the split below | median **321** plays |
| **WCB** (D-I women's basketball) | ~55 D-I games/day | **17.5%** | AP only | median 336 plays |

CFB recaps are long (5,359–9,579 chars). Basketball recaps are short (median ~1,650) but real.

### ⚠️ The men's basketball source split is the whole story

| sample | Data Skrive | AP | no recap |
|---|--:|--:|--:|
| 2023-25 (40 games) | **26** | 11 | 3 |
| 2016-19 (40 games) | 0 | **39** | 1 |

**"Data Skrive" is automated content** — median 1,653 chars, template-shaped. `SCHEMA.md` §7
disqualifies boilerplate/template text, and shipping it would otherwise require
`text_quality: generated` plus sign-off (the gate `05_fnspid` is stuck behind, and the
semi-synthetic text Xinyue rejected).

So `text.source_allowlist` is **on by default** and filters on the publisher's own
`article.source` field — never inferred from the prose. Because the machine copy is a **post-2021
phenomenon**, this costs little on historical seasons and a lot on recent ones. That is the honest
shape of the opportunity: the AP-sourced men's basketball archive is large, and the recent
seasons are mostly not usable.

The demo run makes the filter visible in its own skip counts: **56 `no_report` · 20
`source_not_allowed:Data Skrive` · 2 `short_game`** for 50 kept.

### Scale — deliberately not quoted as a ceiling

At ~64 men's D-I games/day over a ~160-day season this is **order 10k games per season**, and
`51`'s harvest window is 12 seasons. Multiply that out and college basketball is larger than
everything else in the sports tier combined. **This README does not quote that product as a
ceiling**, because:

- the per-day figure is a mean over 11 sampled days with a 10–155 range, not a census;
- the AP share moves with era and needs measuring per season, not once;
- **the CFB count of 960 is itself not yet trustworthy** — an October Saturday returns exactly 25
  completed games with or without `&groups=80/81`, which is low for FBS and suspiciously round.

This file's tier has been wrong by 14× (cricket, down) and 17% (`51`, up) on exactly this kind of
arithmetic. **Census it the way `51` was censused before anyone puts a number in a plan.**

---

## ⚠️ Two discovery traps, both found the hard way

1. **`&groups=50` is mandatory for college basketball.** Without it, 2024-02-10 returns **21**
   completed games; with it, **155** — a **7× undercount that still returns HTTP 200**, no error.
   This is the same failure class as the `&limit=1000` truncation documented in `51`. `groups=50`
   is Division I. Football needs no group filter (verified: default, `groups=80` and `groups=81`
   all return the same count), which is why `params` is per league in the config.
2. **College basketball rejects date *ranges*.** Every monthly range returned 404 while single
   dates work, so there is **no range-query census shortcut here** — the opposite of `51`, whose
   census uses monthly ranges. Discovery is one request per day per league.

Also inherited from `51`: **`site.api.espn.com` returns 403 (Akamai) for every user agent** since
2026-08-12. `site.web.api.espn.com` serves the identical payload and accepts our identifying
research UA — no spoofing.

---

## Not included, and why — both measured, not assumed

The same probe covered the other two un-built claims in the sports tier. Neither is a package,
and neither should be:

- **ESPN MLB (~17.2k claimed) → ~900 real.** Measured recap coverage **2.5%** (1 of 40): the
  summary payload has **no `article` key at all** on 39 of 40 games sampled. Its series half is
  the richest of any league checked (413 plays, 69 win-probability points) — the text is what is
  missing, which is backwards from what the claim assumed. If those ~900 records are wanted, MLB
  is a **US major** and belongs as a fourth league in `51_espn_us_majors`, not here; `51`'s
  builder already handles the flat `plays` shape MLB uses.
- **ESPN soccer (~8k claimed) → 0, on two independent grounds.** (a) **There is no numeric
  series**: soccer summaries carry `commentary` (113–147 entries, which is *text*) and
  `keyEvents` (20–27, of which goals are 2–6). A running scoreline is 2–6 points — far under any
  window floor, and `51`'s own config already rejected period-level granularity for being 3–4
  points. (b) **The rightsholders are different**: Reuters 40/40 for both EPL and UCL, Field
  Level Media 35/40 for MLS. That is two or three *new* licence asks, so it makes the AP request
  harder rather than wider.

---

## Record shape

```json
{
  "text": "<verbatim AP recap>\n\n<ts></ts>",
  "timeseries": [
    {"values": [0, 0, 3, 3, ...], "unit": "away_score_cumulative", "freq": "1play"},
    {"values": [0, 2, 2, 5, ...], "unit": "home_score_cumulative", "freq": "1play"}
  ],
  "alignment": "describes", "license": "proprietary-review",
  "text_source": "third_party", "text_quality": "real",
  "domain": "sports", "region": "US",
  "meta": {"league": "MCB", "report_source": "AP", "n_plays": 326, ...}
}
```

Every play carries a point, not only scoring plays — the flat stretch of a possession that ends
without points is part of what the recap describes. Per `51`'s finding, period-level granularity
gives only 3–4 points for football and was rejected.

**A game is dropped, never patched, when:** it has no recap ≥400 chars · its source is outside
the allowlist · it has <20 plays or <2 periods · **or the play-by-play final disagrees with the
official final** (that last guard is `51`'s, and it catches cases where the series would not land
on the score the prose states).

---

## Pipeline

```bash
pip install -r requirements.txt
python scripts/build_cpt_jsonl.py --dry-run
python scripts/build_cpt_jsonl.py                    # capped demo: 60 total / 20 per league
```

`output.max_records_per_league` exists so a capped demo covers all three leagues: leagues are
walked in order, so a global-only cap fills entirely from football and never exercises the
basketball path or its mandatory `&groups=50`.

## Open items

- **Census before quoting scale** — see above. The day-walk cost is real (one request per day per
  league) and there is no range shortcut for basketball.
- **AP licence** is the only thing between this and a build. Same ask as `51`.
- **Per-season AP share** should be measured across the full window, not just the two era samples
  taken here; the Data Skrive cutover date is the number that sets the usable ceiling.
- **Women's D-I is thin but real** (17.5% coverage, AP-only) and is kept in the league list so its
  yield is measured rather than assumed away.
