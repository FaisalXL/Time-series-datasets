#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from cricket match narration + ball-by-ball series.

RECORD SHAPE IS HYBRID, and the split is measured, not stylistic (see README):

  two-innings formats (T20/IT20/ODI/ODM) -> one record per INNINGS. Its text is the
    passage of the ESPNcricinfo report that narrates that innings, selected by an LLM
    that returns sentence INDICES only (verbatim assembly => text_quality "real") and
    never sees the series. Contract: prompts/attribute_v1.md.

  four-innings formats (TEST/MDM) -> one record per MATCH: the whole-match series (all
    innings concatenated, with an innings_index channel) paired with the whole-match
    recap. Attribution is INVERTED against a permutation control on Tests (7% right
    innings vs 20% wrong), so it is not used there.

SERIES GRANULARITY IS THE SOURCE'S OWN. Cricsheet ships one CSV row per DELIVERY; the
first version of this builder aggregated 6:1 into overs (`over = int(float(ball))`), which
is a transform we were imposing on the data rather than something the data did. Records now
carry the delivery-level series (`1play`). Per-over remains available via
`shape.series_granularity: per_over` and is derivable from the raw archive at any time, but
it is no longer what gets built.

Series: Cricsheet bulk per-delivery CSV (ODC-BY 1.0). Text: ESPNcricinfo report prose.
Cricsheet match_id == ESPNcricinfo match id -> the join key is exact.

⚠️ LICENSE: ESPNcricinfo report prose is copyrighted / ToS-restricted. Every record is
   tagged `proprietary-review`, which SCHEMA.md §6 defines as excluded from any release
   until cleared. Building is permitted; releasing is not. See NOTION_PAGE.md.

NOTE on the window floor: aggregating to overs put a T20 innings at exactly 20 steps,
   below the 32-step floor used elsewhere in the corpus, which made this package swing 3x
   on a decision it had no business depending on (6,885 vs 20,678 records). At delivery
   granularity a T20 innings is ~125 steps and the question does not arise.
   run_report.json still reports the count at every candidate floor.

  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=5
  python scripts/attribute.py                              # LLM pass (cached, resumable)
  python scripts/build_cpt_jsonl.py --set output.max_records=null
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from cricket_lib import (MULTI_INNINGS, ROOT, TWO_INNINGS, http_get,  # noqa: E402
                         innings_table, load_report, parse_info, report_date,
                         sentences, strip_html)
from emit import emit_record  # noqa: E402

DEFAULT_CONFIG = ROOT / "config.example.yaml"
# Team scores are written with the word "for"; bowling figures use a hyphen ("Lyon 3-48").
# Including the hyphen form would inject false positives — measured at 8 per 874 matches
# where a bowling figure coincides with a real innings total — so it is excluded.
SCORE_PAIR = re.compile(r"\b(\d{1,3})\s+for\s+(\d{1,3})\b")
CANDIDATE_FLOORS = (12, 16, 20, 24, 32)

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:                                     # pragma: no cover
    _ENC = None


def ntok(s: str) -> int:
    return len(_ENC.encode(s)) if _ENC else max(1, len(s) // 4)


# --- config helpers (same conventions as the other packages) ---------------

def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    m = dict(base)
    for k, v in over.items():
        m[k] = deep_merge(m[k], v) if k in m and isinstance(m[k], dict) and isinstance(v, dict) else v
    return m


def coerce(raw: str) -> Any:
    low = raw.strip().lower()
    if low in {"true", "yes"}: return True
    if low in {"false", "no"}: return False
    if low in {"null", "none", "~"}: return None
    if re.fullmatch(r"-?\d+", raw): return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw): return float(raw)
    return raw


def parse_sets(sets: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for it in sets:
        k, v = it.split("=", 1)
        cur = out
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = coerce(v)
    return out


def load_config(path: Path, sets: Sequence[str]) -> Dict[str, Any]:
    cfg = yaml.safe_load(path.read_text())
    return deep_merge(cfg, parse_sets(sets)) if sets else cfg


def rp(s: str) -> Path:
    p = Path(s)
    return p if p.is_absolute() else ROOT / p


# --- alignment: measured per record, never asserted ------------------------

def recited_scores(text: str) -> set:
    """(runs, wickets) pairs the prose states, e.g. '187 for 3' or '6 for 194'.

    An ordered PAIR is the point: loose single-number matching on cricket prose has a
    high coincidence floor (a report is full of two- and three-digit numbers), so a bare
    total is only accepted for an all-out innings, where the source's own convention
    drops the wicket count ('Cape Cobras 146').

    BOTH score conventions appear in ESPN copy and the order flips between them:
    English "194 for 4" is runs-first, Australian "6 for 194" is wickets-first, and the
    latter is 11% of resolvable team scores — reading only the first would silently
    under-count `recites`. They are disambiguated by the fact that a completed innings
    cannot lose more than 10 wickets, so the reading is only ambiguous when both numbers
    are <= 10 (1 occurrence in 874 matches), where both are admitted.
    """
    out = set()
    for a, b in SCORE_PAIR.findall(text):
        a, b = int(a), int(b)
        if b <= 10:
            out.add((a, b))       # runs-first: "194 for 4"
        if a <= 10:
            out.add((b, a))       # wickets-first: "6 for 194"
    return out


def recites_innings(text: str, total_runs: int, wickets: int) -> bool:
    if (total_runs, wickets) in recited_scores(text):
        return True
    if wickets >= 10:                       # all out: written as a bare total
        return bool(re.search(rf"\b{total_runs}\b", text))
    return False


def measure_alignment(text: str, innings: List[dict]) -> str:
    """`recites` only if the prose states EVERY innings total the record's series carries."""
    if innings and all(recites_innings(text, t["total_runs"], t["wickets"]) for t in innings):
        return "recites"
    return "describes"


# --- text assembly ---------------------------------------------------------

def budget_text(sents: List[str], keep: List[int], priority: List[int],
                max_tokens: Optional[int]) -> Tuple[str, int]:
    """Assemble `keep` in DOCUMENT order; if over budget drop the LEAST important first.

    `priority` is most-important-first and victims are taken from its tail. Chronology is
    preserved in the output because the series is chronological; what goes when the budget
    binds is what was ranked last — never a tail cut mid-narrative.
    """
    chosen = list(keep)
    dropped = 0
    if max_tokens:
        order = [i for i in priority if i in set(chosen)]
        order += [i for i in chosen if i not in set(order)]   # unranked -> dropped first
        while chosen and ntok(" ".join(sents[i] for i in sorted(chosen))) > max_tokens and len(chosen) > 1:
            victim = order.pop() if order else sorted(chosen)[-1]
            if victim in chosen:
                chosen.remove(victim)
                dropped += 1
    return " ".join(sents[i] for i in sorted(chosen)), dropped


def truncate_sentences(text: str, max_tokens: Optional[int]) -> Tuple[str, int]:
    """Sentence-boundary truncation — the blessed remedy for `describes` over the cap."""
    if not max_tokens or ntok(text) <= max_tokens:
        return text, 0
    sents = sentences(text)
    out: List[str] = []
    for s in sents:
        if ntok(" ".join(out + [s])) > max_tokens and out:
            break
        out.append(s)
    return " ".join(out), len(sents) - len(out)


# --- record construction ---------------------------------------------------

def channels_for_innings(t: dict, granularity: str = "per_over") -> List[dict]:
    """Per-innings channels at over or delivery granularity.

    `per_delivery` exists because a T20 innings is exactly 20 per-over steps — below the
    32-step window floor used elsewhere in the corpus — but ~125 deliveries. Measured over
    the full build: at floor 32, per-over keeps 6,885 of 20,678 records and per-delivery
    keeps 20,675. `1play` is already in validate.py's FREQ_RE, so this needs no schema
    change, and the last `cumulative_runs*` value is the same innings total either way, so
    the alignment tier and its permutation control are unaffected.
    """
    if granularity == "per_delivery":
        return [
            {"values": t["runs_per_ball"], "unit": "runs_per_ball", "freq": "1play"},
            {"values": t["wickets_per_ball"], "unit": "wickets_per_ball", "freq": "1play"},
            {"values": t["cumulative_runs_ball"], "unit": "cumulative_runs", "freq": "1play"},
            {"values": t["run_rate_ball"], "unit": "run_rate", "freq": "1play"},
        ]
    return [
        {"values": t["runs_per_over"], "unit": "runs_per_over", "freq": "1over"},
        {"values": t["wickets_per_over"], "unit": "wickets_per_over", "freq": "1over"},
        {"values": t["cumulative_runs"], "unit": "cumulative_runs", "freq": "1over"},
        {"values": t["run_rate"], "unit": "run_rate", "freq": "1over"},
    ]


def channels_for_match(table: List[dict], granularity: str = "per_delivery") -> List[dict]:
    """Whole-match series: innings concatenated, cumulative/run-rate reset per innings.

    Same granularity rule as the per-innings shape — a delivery is the unit Cricsheet
    actually ships, and per-over is a 6:1 aggregation we would be imposing on it.
    """
    if granularity == "per_delivery":
        runs, wkts, cum, rr, idx = [], [], [], [], []
        for k, t in enumerate(table, start=1):
            runs += t["runs_per_ball"]
            wkts += t["wickets_per_ball"]
            cum += t["cumulative_runs_ball"]
            rr += t["run_rate_ball"]
            idx += [k] * t["balls"]
        return [
            {"values": runs, "unit": "runs_per_ball", "freq": "1play"},
            {"values": wkts, "unit": "wickets_per_ball", "freq": "1play"},
            {"values": cum, "unit": "cumulative_runs_in_innings", "freq": "1play"},
            {"values": rr, "unit": "run_rate_in_innings", "freq": "1play"},
            {"values": idx, "unit": "innings_index", "freq": "1play"},
        ]
    runs, wkts, cum, rr, idx = [], [], [], [], []
    for k, t in enumerate(table, start=1):
        runs += t["runs_per_over"]
        wkts += t["wickets_per_over"]
        cum += t["cumulative_runs"]
        rr += t["run_rate"]
        idx += [k] * t["overs"]
    return [
        {"values": runs, "unit": "runs_per_over", "freq": "1over"},
        {"values": wkts, "unit": "wickets_per_over", "freq": "1over"},
        {"values": cum, "unit": "cumulative_runs_in_innings", "freq": "1over"},
        {"values": rr, "unit": "run_rate_in_innings", "freq": "1over"},
        {"values": idx, "unit": "innings_index", "freq": "1over"},
    ]


def common_meta(mid: str, info: dict, art: dict) -> Dict[str, Any]:
    return {
        "match_id": mid,
        "match_type": (info.get("match_type") or "").upper(),
        "gender": info.get("gender"),
        "event": info.get("event"),
        "season": info.get("season"),
        "venue": info.get("venue"),
        "city": info.get("city"),
        "winner": info.get("winner"),
        "toss_winner": info.get("toss_winner"),
        "report_headline": art.get("headline"),
        "report_byline": art.get("byline"),
        # `published` is a CMS re-stamp (wrong by >30d in 72% of articles); this is the real one
        "report_posted": report_date(art),
        "report_type": art.get("type"),
    }


def build_match(mid: str, dcsv: bytes, icsv: bytes, cfg: Dict[str, Any],
                cache: Path, stat: Counter) -> List[dict]:
    d, t, sh = cfg["data"], cfg["text"], cfg["shape"]
    info = parse_info(icsv)
    table = innings_table(dcsv)
    if not table:
        stat["no_innings"] += 1
        return []
    fmt = (info.get("match_type") or "").upper()
    text, art = load_report(mid, d, cache)
    if not text or len(text) < int(t["min_report_chars"]):
        stat["no_report"] += 1
        return []

    url = d["espn_report_url_template"].format(match_id=mid)
    dates = info.get("dates") or [info.get("date")]
    p_start = (dates[0] or "").replace("/", "-") or None
    p_end = (dates[-1] or "").replace("/", "-") or None
    maxtok = t.get("max_tokens")
    per_innings = (fmt in TWO_INNINGS and sh.get("two_innings_mode") == "per_innings")
    # per-match (TEST/MDM) always stays per-over: those series already run ~360 steps, and
    # per-delivery would turn a whole Test into ~2,700 points at 5.3x the datapoints.
    gran = str(sh.get("series_granularity", "per_over"))

    out: List[dict] = []
    if per_innings:
        fp = cache / "attrib" / f"{mid}.json"
        if not fp.exists():
            stat["no_attribution"] += 1
            return []
        att = json.loads(fp.read_text())
        sents = sentences(text)
        if att.get("n_sents") != len(sents):
            stat["attrib_stale"] += 1          # report changed since attribution — redo it
            return []
        shared = [i for i in att.get("shared", []) if i < len(sents)]
        for tb in table:
            inn = tb["innings"]
            own = [i for i in att.get("innings", {}).get(str(inn), []) if i < len(sents)]
            if tb["overs"] < int(d["min_overs"]):
                stat["short_innings"] += 1
                continue
            if len(own) < int(sh.get("min_attributed_sentences", 2)):
                stat["innings_not_narrated"] += 1
                continue
            keep = sorted(set(own) | set(shared))
            # Most-important-first. The lede (shared[0]) leads: it is the sentence that
            # names both sides and recites the innings totals, so it is what grounds the
            # record and the last thing that should go. Then this innings' own narrative in
            # the model's ranking, then the rest of the shared bucket — whose tail is the
            # contested/unassigned prose swept in by the repair, i.e. the cheapest to lose.
            priority = (shared[:1] + own + shared[1:])
            body, dropped = budget_text(sents, keep, priority, maxtok)
            stat["budget_dropped_sentences"] += dropped
            align = measure_alignment(body, [tb])
            meta = common_meta(mid, info, art)
            meta.update({
                "record_shape": "per_innings", "innings": int(inn),
                "batting_team": tb["batting_team"], "bowling_team": tb["bowling_team"],
                "overs_bowled": tb["overs"], "total_runs": tb["total_runs"],
                "wickets": tb["wickets"],
                "text_sentences_own": len(own), "text_sentences_shared": len(shared),
                "series_granularity": gran, "balls": tb["balls"],
                "attribution": "prompts/attribute_v1.md",
            })
            out.append(emit_record(
                text=f"{body}\n\n<ts></ts>", timeseries=channels_for_innings(tb, gran),
                alignment=align, license="proprietary-review", text_source="third_party",
                source=url, dataset="cricket_report_overseries",
                series_id=f"cricket_report_overseries:{mid}:inn{inn}",
                domain="sports", region="global",
                period_start=p_start, period_end=p_end, meta=meta))
            stat[f"emit_{align}"] += 1
    else:
        total_overs = sum(tb["overs"] for tb in table)
        # gate on overs regardless of granularity: min_overs is a cricket threshold
        # ("drop rain-ruined matches"), not a series-length floor
        if total_overs < int(d["min_overs"]):
            stat["short_match"] += 1
            return []
        body, dropped = truncate_sentences(text, maxtok)
        stat["truncated_sentences"] += dropped
        if dropped:
            stat["records_truncated"] += 1
        align = measure_alignment(body, table)
        meta = common_meta(mid, info, art)
        meta.update({
            "record_shape": "per_match", "innings_count": len(table),
            "overs_total": total_overs,
            "series_granularity": gran,
            "innings_totals": [{"innings": int(tb["innings"]), "batting_team": tb["batting_team"],
                                "runs": tb["total_runs"], "wickets": tb["wickets"],
                                "overs": tb["overs"]} for tb in table],
        })
        out.append(emit_record(
            text=f"{body}\n\n<ts></ts>", timeseries=channels_for_match(table, gran),
            alignment=align, license="proprietary-review", text_source="third_party",
            source=url, dataset="cricket_report_overseries",
            series_id=f"cricket_report_overseries:{mid}:match",
            domain="sports", region="global",
            period_start=p_start, period_end=p_end, meta=meta))
        stat[f"emit_{align}"] += 1
    return out


# --- permutation control ---------------------------------------------------

def permutation_control(records: List[dict]) -> Dict[str, Any]:
    """The corpus standard for an alignment claim (#08: 20.1% true vs 0.0% permuted).

    TRUE   : does this record's own prose recite its own series' innings total(s)?
    CONTROL: does it recite the totals of a DIFFERENT match of the same format?
    A control near the true rate means the match is coincidence, not alignment.
    """
    by_fmt: Dict[str, List[dict]] = defaultdict(list)
    for r in records:
        by_fmt[r["meta"]["match_type"]].append(r)
    rows = {}
    for fmt, rs in by_fmt.items():
        if len(rs) < 2:
            continue
        true_hit = ctrl_hit = n_ctrl = 0
        for i, r in enumerate(rs):
            # The control partner must be a DIFFERENT MATCH. Records are emitted in match
            # order, so the neighbour of a per-innings record is the *other innings of its
            # own match* — whose total the shared lede legitimately recites. Pairing with
            # that would measure the lede, not coincidence, and silently inflate the control.
            other = None
            for step in range(1, len(rs)):
                cand = rs[(i + step) % len(rs)]
                if cand["meta"]["match_id"] != r["meta"]["match_id"]:
                    other = cand
                    break
            if other is None:
                continue
            n_ctrl += 1
            tots = ([{"total_runs": r["meta"]["total_runs"], "wickets": r["meta"]["wickets"]}]
                    if r["meta"]["record_shape"] == "per_innings"
                    else [{"total_runs": x["runs"], "wickets": x["wickets"]}
                          for x in r["meta"]["innings_totals"]])
            otots = ([{"total_runs": other["meta"]["total_runs"], "wickets": other["meta"]["wickets"]}]
                     if other["meta"]["record_shape"] == "per_innings"
                     else [{"total_runs": x["runs"], "wickets": x["wickets"]}
                           for x in other["meta"]["innings_totals"]])
            txt = r["text"]
            true_hit += all(recites_innings(txt, t["total_runs"], t["wickets"]) for t in tots)
            ctrl_hit += all(recites_innings(txt, t["total_runs"], t["wickets"]) for t in otots)
        n = len(rs)
        if not n_ctrl:
            continue
        rows[fmt] = {"n": n, "n_control": n_ctrl,
                     "true": round(true_hit / n, 4), "control": round(ctrl_hit / n_ctrl, 4),
                     "lift_pp": round(100 * (true_hit / n - ctrl_hit / n_ctrl), 1)}
    return rows


def floor_exposure(records: List[dict]) -> Dict[str, Any]:
    """How many records survive each candidate window floor (the open #58 decision)."""
    lens = [len(r["timeseries"][0]["values"]) for r in records]
    return {str(f): sum(1 for x in lens if x >= f) for f in CANDIDATE_FLOORS}


# --- pipeline --------------------------------------------------------------

def run(cfg: Dict[str, Any], dry: bool) -> Dict[str, Any]:
    d, out_cfg = cfg["data"], cfg["output"]
    cache = rp(d["cache_dir"])
    maxrec = out_cfg.get("max_records")

    zpath = cache / "all_csv2.zip"
    if not zpath.exists():
        zpath.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {d['cricsheet_zip_url']} (cached after first run)...", file=sys.stderr)
        zpath.write_bytes(http_get(d["cricsheet_zip_url"], d["user_agent"], int(d["timeout_s"])))
    z = zipfile.ZipFile(zpath)
    names = set(z.namelist())
    mids = sorted(n[:-4] for n in names
                  if n.endswith(".csv") and not n.endswith("_info.csv"))

    stat: Counter = Counter()
    records: List[dict] = []
    for mid in mids:
        if f"{mid}_info.csv" not in names:
            stat["no_info"] += 1
            continue
        stat["matches_scanned"] += 1
        try:
            recs = build_match(mid, z.read(f"{mid}.csv"), z.read(f"{mid}_info.csv"),
                               cfg, cache, stat)
        except ValueError as e:                       # emit_record rejected it — loud, not silent
            stat["invalid"] += 1
            if stat["invalid"] <= 3:
                print(f"  invalid {mid}: {e}", file=sys.stderr)
            continue
        records.extend(recs)
        if maxrec is not None and len(records) >= int(maxrec):
            records = records[:int(maxrec)]
            break

    fmt_counts = Counter(r["meta"]["match_type"] for r in records)
    shape_counts = Counter(r["meta"]["record_shape"] for r in records)
    toks = [ntok(r["text"].replace("<ts></ts>", "")) for r in records]
    toks_sorted = sorted(toks)
    report = {
        "cricsheet_zip_url": d["cricsheet_zip_url"],
        "report_source": "site.web.api.espn.com (site.api.espn.com 403s Akamai since 2026-08)",
        "record_shape": dict(cfg["shape"]),
        "stats": dict(stat),
        "records": len(records),
        "by_format": dict(fmt_counts),
        "by_shape": dict(shape_counts),
        "alignment": {"recites": stat.get("emit_recites", 0),
                      "describes": stat.get("emit_describes", 0)},
        "permutation_control": permutation_control(records) if records else {},
        "window_floor_exposure": floor_exposure(records) if records else {},
        "text_tokens": {
            "median": toks_sorted[len(toks_sorted) // 2] if toks else 0,
            "p90": toks_sorted[int(0.9 * len(toks_sorted))] if toks else 0,
            "over_500": sum(1 for x in toks if x > 500),
        },
        "config_snapshot": cfg,
        "dry_run": dry,
    }

    if dry:
        if records:
            print("\n--- sample record ---")
            print(json.dumps(records[0], ensure_ascii=False, indent=2)[:2600])
        print("\n" + json.dumps({k: v for k, v in report.items()
                                 if k not in ("config_snapshot",)}, indent=2))
        return report

    op = rp(out_cfg["output_path"]); op.parent.mkdir(parents=True, exist_ok=True)
    with op.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    if records and out_cfg.get("samples_path"):
        sp = rp(out_cfg["samples_path"]); sp.parent.mkdir(parents=True, exist_ok=True)
        with sp.open("w", encoding="utf-8") as fh:
            json.dump(records[:3], fh, ensure_ascii=False, indent=2); fh.write("\n")
    rpath = rp(out_cfg["report_path"]); rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Build cricket narration + per-over CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    rep = run(load_config(args.config, args.set), dry=args.dry_run)
    s = rep["stats"]
    print(f"\nDone: {rep['records']} records "
          f"(matches {s.get('matches_scanned', 0)}, no_report {s.get('no_report', 0)}, "
          f"no_attribution {s.get('no_attribution', 0)}, invalid {s.get('invalid', 0)}) "
          f"— recites {rep['alignment']['recites']} / describes {rep['alignment']['describes']}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
