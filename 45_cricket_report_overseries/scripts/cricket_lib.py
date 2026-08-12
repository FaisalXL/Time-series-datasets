#!/usr/bin/env python3
"""Shared parsing for the cricket package: Cricsheet CSV, ESPN payloads, sentences.

Used by both `attribute.py` (the LLM pass) and `build_cpt_jsonl.py` (the builder) so the
two can never disagree about what a sentence, an innings or a wicket is.
"""
from __future__ import annotations

import csv
import html as _html
import io
import json
import re
import ssl
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]

# Two-innings formats get per-innings records via LLM attribution; four-innings formats
# are built per-match (see prompts/attribute_v1.md, "Why not Tests").
TWO_INNINGS = {"T20", "IT20", "ODI", "ODM"}
MULTI_INNINGS = {"TEST", "MDM"}

# Cricsheet writes these in `wicket_type`, but they are NOT dismissals: the batter is not
# out and the scorecard does not read "for N+1". Counting them inflates `wickets_per_over`
# and breaks the exact figure the report states ("152 for 8" vs a naive count of 9).
# `retired out` IS a dismissal and stays counted.
NOT_DISMISSALS = {"retired hurt", "retired not out"}

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


# --- Cricsheet -------------------------------------------------------------

def parse_info(raw: bytes) -> Dict[str, Any]:
    """{match_id}_info.csv -> dict. `team` and `date` collect into lists (multi-day
    matches list one `date` row per day; the builder needs first and last)."""
    info: Dict[str, Any] = {"team": [], "dates": []}
    for row in csv.reader(io.StringIO(raw.decode("utf-8", "replace"))):
        if len(row) >= 3 and row[0] == "info":
            key, val = row[1], row[2]
            if key == "team":
                info["team"].append(val)
            elif key == "date":
                info["dates"].append(val)
                info.setdefault("date", val)
            elif key not in info:
                info[key] = val
    return info


def _num(s) -> float:
    try:
        return float(s) if s not in ("", "None", None) else 0.0
    except (TypeError, ValueError):
        return 0.0


def innings_table(raw: bytes) -> List[Dict[str, Any]]:
    """-> ordered list of innings, with per-over AND per-delivery channels, plus totals.

    over index = int(float(ball)); runs = runs_off_bat + extras; a wicket is any
    `wicket_type` outside NOT_DISMISSALS.

    Both granularities are computed in one pass because the caller picks between them per
    record: a T20 innings is 20 per-over steps but ~125 per-delivery steps, and only the
    latter clears the corpus window floor. The two are consistent by construction — the
    last value of `cumulative_runs` and `cumulative_runs_ball` is the same innings total,
    which is what the prose recites and what the alignment check tests.
    """
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))
    order: List[str] = []
    runs: Dict[str, Dict[int, int]] = {}
    wkts: Dict[str, Dict[int, int]] = {}
    ball_runs: Dict[str, List[int]] = {}
    ball_wkts: Dict[str, List[int]] = {}
    teams: Dict[str, Tuple[str, str]] = {}
    for r in rows:
        inn = r.get("innings")
        if inn is None:
            continue
        if inn not in runs:
            order.append(inn)
            runs[inn], wkts[inn] = {}, {}
            ball_runs[inn], ball_wkts[inn] = [], []
            teams[inn] = (r.get("batting_team", ""), r.get("bowling_team", ""))
        try:
            o = int(float(r["ball"]))
        except (KeyError, TypeError, ValueError):
            continue
        v = int(_num(r.get("runs_off_bat")) + _num(r.get("extras")))
        wt = (r.get("wicket_type") or "").strip().lower()
        w = 1 if (wt and wt not in NOT_DISMISSALS) else 0
        runs[inn][o] = runs[inn].get(o, 0) + v
        if w:
            wkts[inn][o] = wkts[inn].get(o, 0) + 1
        # per-delivery keeps CSV row order: every ball bowled, wides and no-balls included
        ball_runs[inn].append(v)
        ball_wkts[inn].append(w)

    out = []
    for inn in order:
        n = max(runs[inn]) + 1 if runs[inn] else 0
        rpo = [runs[inn].get(i, 0) for i in range(n)]
        wpo = [wkts[inn].get(i, 0) for i in range(n)]
        cum, s = [], 0
        for x in rpo:
            s += x
            cum.append(s)
        rr = [round(cum[i] / (i + 1), 2) for i in range(n)]

        br, bw = ball_runs[inn], ball_wkts[inn]
        bcum, s2 = [], 0
        for x in br:
            s2 += x
            bcum.append(s2)
        # run rate stays runs-per-OVER even at delivery granularity — it is the quantity
        # the reports talk about, and per-ball rate would not be comparable across records
        brr = [round(bcum[i] / ((i + 1) / 6.0), 2) for i in range(len(br))]

        out.append({
            "innings": inn, "batting_team": teams[inn][0], "bowling_team": teams[inn][1],
            "overs": n, "runs_per_over": rpo, "wickets_per_over": wpo,
            "cumulative_runs": cum, "run_rate": rr,
            "balls": len(br), "runs_per_ball": br, "wickets_per_ball": bw,
            "cumulative_runs_ball": bcum, "run_rate_ball": brr,
            "total_runs": cum[-1] if cum else 0, "wickets": sum(wpo),
        })
    return out


# --- ESPN ------------------------------------------------------------------

def strip_html(s: str) -> str:
    """ESPN `article.story` is HTML with `<video1>`/`<photo1>` placeholder tags."""
    s = re.sub(r"<(?:video|photo|inline)\d*[^>]*>", "", s)
    s = re.sub(r"</(?:p|h[1-6]|li|div|ul|ol)>", "\n\n", s)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def http_get(url: str, ua: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL).read()


def load_report(match_id: str, d: dict, cache: Path) -> Tuple[Optional[str], dict]:
    """Report text + article meta for a match, cached per match_id.

    The cache stores `{"article": ...}` only — the sole key read here — which keeps a
    full-archive cache near 200 MB instead of ~11 GB of untrimmed payloads.
    """
    fp = cache / "espn" / f"{match_id}.json"
    if fp.exists():
        try:
            art = json.loads(fp.read_text()).get("article") or {}
        except Exception:
            return None, {}
    else:
        url = d["espn_summary_url"].format(league=d["espn_carrier_league"], match_id=match_id)
        try:
            raw = http_get(url, d["user_agent"], int(d["timeout_s"]))
        except Exception:
            return None, {}
        art = (json.loads(raw).get("article") or {})
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps({"article": art}, ensure_ascii=False))
        time.sleep(float(d.get("request_delay_s", 0.4)))
    story = strip_html(art.get("story") or "")
    return (story or None), art


def sentences(text: str) -> List[str]:
    """Split into sentences, preserving paragraph boundaries."""
    out: List[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])", para):
            s = s.strip()
            if s:
                out.append(s)
    return out


def report_date(art: dict) -> Optional[str]:
    """The report's real posting date.

    `article.published` is a CMS re-stamp — it disagrees with the original posting by more
    than 30 days in 72% of articles (a 2018 match carries `published: 2019-03-18`), so it
    must not be recorded as the report date. `originallyPosted` is the true one.
    """
    for k in ("originallyPosted", "published"):
        v = art.get(k)
        if v:
            return str(v)[:10]
    return None
