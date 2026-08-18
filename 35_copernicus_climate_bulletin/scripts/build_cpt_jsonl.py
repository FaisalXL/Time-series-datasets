#!/usr/bin/env python3
"""Build CPT world-knowledge JSONL from the Copernicus C3S Climate Bulletin.

One record = **(theme x reported month x the bulletin's own narrative section)**: that
section's prose, verbatim, paired with the series behind *that section's own figure* --
C3S publishes the CSV for every figure it draws, so the numbers are the ones the prose was
written against, with no third-party series to reconcile.

Three things changed from the banked build:

1. **The hydrological theme was missing entirely.** The bulletin navigation on every page
   names three themes -- Surface air temperature / Sea ice cover / Hydrological variables --
   and only two were built. Hydrological is 111 months with its own figure data: global and
   European relative humidity monthly since 1979, plus four-month means of precipitation,
   soil moisture, temperature and humidity for four European sub-regions.

2. **The text era started 41 months late and the record unit was the whole page.** The
   universe runs **2015-08 -> 2026-06** across six URL slugs (see `c3ssrc`), not 2019-01, and
   each page is natively sectioned as a topic x period grid which the banked build collapsed
   into one record -- the corpus audit measured it at ~1.9 topics per bulletin.

3. **The temperature series was an expanding window** of 505-1038 monthly points, re-shipping
   the whole ERA5 history every month. The bulletin's headline claim is a *rank within a
   calendar month* ("the sixth warmest January on record") and C3S publishes exactly that
   series -- one point per year for that month, 1940->present. A section about the reported
   month now gets the calendar-month-across-years series its claim is about, which is both the
   aligned pairing and a bounded one. The sea-ice half of the banked build already worked this
   way; this generalises it to temperature.

Usage:
  python scripts/harvest.py                 # pages + figure CSVs (cached)
  python scripts/build_cpt_jsonl.py --dry-run --set output.max_records=4
  python scripts/build_cpt_jsonl.py
  python scripts/build_cpt_jsonl.py --audit-alignment
  python scripts/build_cpt_jsonl.py --audit-vintage    # ERA5 revision between bulletins
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c3sdata                                            # noqa: E402
import c3ssec                                             # noqa: E402
import c3ssrc                                             # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
from emit import emit_record                              # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"


# --- config ---------------------------------------------------------------

def deep_merge(base: dict, over: dict) -> dict:
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


def parse_sets(sets: Sequence[str]) -> dict:
    out: dict = {}
    for it in sets:
        k, v = it.split("=", 1)
        cur = out
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = coerce(v)
    return out


def load_config(path: Path, sets: Sequence[str]) -> dict:
    cfg = yaml.safe_load(path.read_text())
    return deep_merge(cfg, parse_sets(sets)) if sets else cfg


def rp(s: str) -> Path:
    p = Path(s)
    return p if p.is_absolute() else ROOT / p


# --- which series does a section get? -------------------------------------

class Rule(NamedTuple):
    kinds: Tuple[str, ...]    # figure-data families, in preference order
    domains: Tuple[str, ...]  # one channel group per domain
    stride: str               # 'annual' | 'monthly' | 'daily'


#: (theme, topic, period) -> the figure-data family that section's figure is drawn from.
#: `period == 'month'` means the section is about the reported month itself, which is where
#: the bulletin makes its rank-in-the-record claim, so those get the calendar-month series.
#:
#: The second entry of `kinds` is a fallback that matters: C3S only began publishing the
#: dedicated calendar-month CSV (`..._global_June_DATA.csv`) in the recent era, but the
#: `allmonths` file it has always published contains the same numbers -- selecting one month
#: across years from it reproduces the calendar-month figure exactly, which is what the
#: bulletin itself plots. Without the fallback the whole pre-2024 temperature era would drop
#: for want of a file, having had the data all along.
RULES: Dict[Tuple[str, str, str], Rule] = {}


def _add(theme, topics, periods, kinds, domains, stride):
    for t in topics:
        for p in periods:
            RULES[(theme, t, p)] = Rule(tuple(kinds), tuple(domains), stride)


_ALL_PERIODS = ["month", "12months", "season", "ytd", "other"]

_add("temperature", ["global"], ["month"],
     ["sat_calendar_month", "sat_allmonths"], ["global"], "annual")
_add("temperature", ["europe"], ["month"],
     ["sat_calendar_month", "sat_allmonths"], ["europe"], "annual")
_add("temperature", ["global"], ["12months"],
     ["sat_12month", "sat_allmonths"], ["global"], "monthly")
_add("temperature", ["europe"], ["12months"],
     ["sat_12month", "sat_allmonths"], ["europe"], "monthly")
_add("temperature", ["global"], ["season", "ytd", "other"],
     ["sat_allmonths"], ["global"], "monthly")
_add("temperature", ["europe"], ["season", "ytd", "other"],
     ["sat_allmonths"], ["europe"], "monthly")
_add("temperature", ["regional"], _ALL_PERIODS,
     ["sat_allmonths"], ["global", "europe"], "monthly")
_add("temperature", ["sst"], _ALL_PERIODS, ["sst_daily"], ["60s-60n"], "daily")
_add("sea_ice", ["arctic"], _ALL_PERIODS, ["sie_calendar_month"], ["arctic"], "annual")
_add("sea_ice", ["antarctic"], _ALL_PERIODS, ["sie_calendar_month"], ["antarctic"], "annual")
_add("hydrological", ["europe"], _ALL_PERIODS, ["hydro_4month"],
     ["nweurope", "neeurope", "sweurope", "seeurope"], "monthly")
_add("hydrological", ["global", "trends"], _ALL_PERIODS, ["rh_monthly"],
     ["global_and_europe"], "monthly")


# --- the cached corpus ----------------------------------------------------

class Bulletin(NamedTuple):
    theme: str
    ym: str
    html_path: Path
    csv_names: List[str]


def load_index(cfg) -> Dict[str, List[str]]:
    """page filename -> the cache names of the CSVs that page links.

    Derived from the cached HTML rather than read from `csv_index.json`. The index is a pure
    function of the pages, so deriving it removes an ordering dependency on the harvest (the
    JSON is only written when the CSV pass finishes, and a builder that read it early saw an
    empty index and dropped every section for "no series").
    """
    import harvest
    html_dir = rp(cfg["data"]["html_cache_dir"])
    out: Dict[str, List[str]] = {}
    for p in sorted(html_dir.glob("*.html")):
        out[p.name] = [harvest.csv_local(u) for u in harvest.csv_urls_from(p.read_bytes())]
    return out


def bulletins(cfg) -> List[Bulletin]:
    html_dir = rp(cfg["data"]["html_cache_dir"])
    idx = load_index(cfg)
    out = []
    for p in sorted(html_dir.glob("*.html")):
        theme, ymc = p.stem.rsplit("_", 1)
        out.append(Bulletin(theme, f"{ymc[:4]}-{ymc[4:]}", p, idx.get(p.name, [])))
    return out


_TABLE_CACHE: Dict[str, Optional[c3sdata.Table]] = {}
_FAM_CACHE: Dict[str, Optional[c3sdata.Family]] = {}


def load_table(cfg, name: str) -> Optional[c3sdata.Table]:
    if name not in _TABLE_CACHE:
        p = rp(cfg["data"]["csv_cache_dir"]) / name
        _TABLE_CACHE[name] = c3sdata.parse(p.read_bytes()) if p.exists() else None
    return _TABLE_CACHE[name]


def family_of(cfg, name: str) -> Optional[c3sdata.Family]:
    if name not in _FAM_CACHE:
        tab = load_table(cfg, name)
        fam = None if tab is None else (
            c3sdata.classify(name) or c3sdata.classify_content(tab, name))
        if fam is not None and not fam.baseline:
            fam = fam._replace(baseline=c3sdata.baseline_of(tab))
        _FAM_CACHE[name] = fam
    return _FAM_CACHE[name]


def pick_series(cfg, bul: Bulletin, rule: Rule, month_no: int, want_baseline: str = ""
                ) -> List[Tuple[str, c3sdata.Table, str, c3sdata.Family]]:
    """(domain, table, column, family) triples backing this section, from THIS bulletin's CSVs.

    `want_baseline` is the reference period the section's own prose quotes against. C3S moved
    its headline baseline from 1981-2010 to 1991-2020 and publishes both files for many
    months, so preferring the wrong one pairs "0.46°C warmer than the 1981-2010 average" with
    a 1991-2020 series -- the largest genuine cause of prose figures missing from the attached
    series. Falls back to 1991-2020 when the prose names no baseline.
    """
    prefer = want_baseline or "1991-2020"
    out = []
    for dom in rule.domains:
        chosen: Optional[Tuple[c3sdata.Table, c3sdata.Family]] = None
        for kind in rule.kinds:                      # preference order, fallback second
            cands = []
            for name in bul.csv_names:
                fam = family_of(cfg, name)
                tab = load_table(cfg, name)
                if fam is None or tab is None or fam.kind != kind or fam.domain != dom:
                    continue
                if rule.stride == "annual" and kind.endswith("calendar_month") \
                        and fam.calendar_month not in (None, month_no):
                    continue
                cands.append((tab, fam))
            if cands:
                # Preference order matters more than coverage here. A bulletin links several
                # files of the same family that hold *different quantities*: for sea ice, an
                # extent file (`CIE`, columns "SIE anomaly"...) and an area file (`CIA`,
                # columns "Arctic","Antarctic"). Sorting by row count picked the area file --
                # 487 rows against 41 -- and paired "0.8 million km2 below average ... third
                # lowest July extent" with an area anomaly of -0.38 whose rank is 10th. So the
                # named quantity is ranked first, then the baseline the prose quotes, and only
                # then coverage.
                cands.sort(key=lambda tf: (not _has_preferred_quantity(kind, tf[0]),
                                           tf[1].baseline != prefer,
                                           tf[1].baseline != "1991-2020",
                                           -len(tf[0].rows)))
                chosen = cands[0]
                break
        if chosen is None:
            continue
        tab, fam = chosen
        cols = c3sdata.pick_columns(fam, tab)
        # A file whose columns name basins/regions (the older sea-ice and temperature files
        # hold Arctic *and* Antarctic, or Global *and* European, in one table) contributes only
        # the column for this record's own region. Publishing all of them put an
        # `antarctic_arctic` channel inside the Antarctic section -- the cross-contamination
        # the per-section design exists to prevent.
        regional = [c for c in cols if c.strip().lower() in _REGION_ALIASES]
        if regional:
            mine = [c for c in regional if _REGION_ALIASES[c.strip().lower()] == dom]
            cols = mine or ([] if fam.kind == "sie_calendar_month" else cols)
        for col in cols:
            out.append((dom, tab, col, fam))
    return out


def window(tab: c3sdata.Table, col: str, ym: str, stride: str, month_no: int,
           n_monthly: int, n_daily: int) -> Tuple[List[Optional[float]], List[str]]:
    """The series slice for this section, oldest -> newest, ending on the reported period."""
    keys = sorted(tab.rows)
    if stride == "annual":
        # one point per year for the reported calendar month: the series the bulletin's
        # rank-in-the-record claim is a claim about.
        sel = [k for k in keys if len(k) == 7 and int(k[5:7]) == month_no and k <= ym]
    elif stride == "daily":
        sel = [k for k in keys if len(k) == 10 and k[:7] <= ym][-n_daily:]
    else:
        sel = [k for k in keys if len(k) == 7 and k <= ym][-n_monthly:]
    return [tab.rows[k].get(col) for k in sel], sel


# --- build ----------------------------------------------------------------

def build(cfg) -> Tuple[List[dict], dict]:
    d, t, o = cfg["data"], cfg["text"], cfg["output"]
    min_chars = int(t["min_text_chars"])
    n_monthly = int(d["window_months"])
    n_daily = int(d["window_days"])
    min_points = int(d["min_points"])
    max_null = float(d["max_null_fraction"])
    require_terminal = bool(d.get("require_terminal_is_reported_month", True))
    maxrec = o.get("max_records")
    stat: collections.Counter = collections.Counter()
    records: List[dict] = []
    seen_text: set = set()
    seen_sid: set = set()

    for bul in bulletins(cfg):
        if bul.theme not in d["themes"]:
            continue
        stat["bulletins"] += 1
        fallback = {"temperature": "global", "sea_ice": "arctic",
                    "hydrological": "europe"}[bul.theme]
        secs = c3ssec.sections(bul.html_path.read_text(errors="ignore"), fallback)
        if not secs:
            stat["drop_bulletin_no_section"] += 1
            continue
        month_no = int(bul.ym[5:7])
        for sec in secs:
            stat["section_units"] += 1
            if len(sec.text) < min_chars:
                stat["drop_short_text"] += 1
                continue
            rule = RULES.get((bul.theme, sec.topic, sec.period))
            if rule is None:
                stat["drop_no_rule"] += 1
                stat[f"norule::{bul.theme}/{sec.topic}/{sec.period}"] += 1
                continue
            named_bases = c3ssec.baselines_named(sec.text)
            triples = pick_series(cfg, bul, rule, month_no,
                                  named_bases[0] if named_bases else "")
            if not triples:
                stat["drop_no_series_for_section"] += 1
                stat[f"noseries::{bul.theme}/{rule.kinds[0]}"] += 1
                continue
            chans = []
            for dom, tab, col, fam in triples:
                vals, keys = window(tab, col, bul.ym, rule.stride, month_no, n_monthly, n_daily)
                if len(vals) < min_points or vals[-1] is None:
                    stat["channel_dropped_short_or_null_end"] += 1
                    continue
                # The series must actually END on the month the bulletin reports. Bulletins
                # sometimes link a figure CSV from an earlier month's folder alongside their
                # own (the 2024-03 hydrological page links 2023-11 files too), and taking the
                # latest value at or before the reported month would then pair this month's
                # prose with a terminal point from an earlier month -- exactly the mismatch
                # `period_end == reported month` is supposed to guarantee.
                if require_terminal and not _terminal_is_reported(keys[-1], bul.ym, rule.stride):
                    stat["channel_dropped_terminal_not_reported_month"] += 1
                    continue
                if sum(1 for v in vals if v is None) / len(vals) > max_null:
                    stat["channel_dropped_over_null_budget"] += 1
                    continue
                unit = (c3sdata.CHANNEL_UNITS.get((fam.kind, col.strip().lower()))
                        or _unit_name(fam, dom, col, tab))
                chans.append((unit, vals, keys, fam, tab))
            if not chans:
                stat["drop_all_channels_unusable"] += 1
                continue
            # Belt and braces on channel units: whatever the naming rules produce, a record
            # must not carry two channels with one unit. Column vocabularies churn between
            # eras, so a naming rule that is complete today can silently collide tomorrow --
            # and the failure mode is the whole record being rejected, not one channel.
            seen_units: Dict[str, int] = {}
            deduped = []
            for u, vv, kk, ff, tb in chans:
                if u in seen_units:
                    seen_units[u] += 1
                    stat["channel_unit_disambiguated"] += 1
                    u = f"{u}_{seen_units[u]}"
                else:
                    seen_units[u] = 0
                deduped.append((u, vv, kk, ff, tb))
            chans = deduped
            # all channels of a record share one length and one grid (SCHEMA: same freq)
            n = min(len(c[1]) for c in chans)
            if n < min_points:
                stat["drop_short_window"] += 1
                continue
            chans = [(u, v[-n:], k[-n:], f, tb) for u, v, k, f, tb in chans]

            freq = {"annual": "1y", "monthly": "1M", "daily": "1d"}[rule.stride]
            text = f"{sec.text}\n\n<ts></ts>"
            sid = (f"{d['series_id_prefix']}_{bul.theme}_{bul.ym}"
                   f"_{sec.topic}_{sec.period}_s{sec.ordinal}")
            if sid in seen_sid:
                stat["drop_duplicate_series_id"] += 1
                continue
            if text in seen_text:
                stat["drop_duplicate_text"] += 1
                continue

            figs = c3ssec.figures(sec.text)
            terminals = [c[1][-1] for c in chans]
            tier = ("recites" if any(c3ssec.quotes(f, tv) for f in figs for tv in terminals)
                    else "describes")
            keys0 = chans[0][2]
            try:
                rec = emit_record(
                    text=text,
                    timeseries=[{"values": [None if v is None else round(v, 4) for v in vv],
                                 "unit": u, "freq": freq} for u, vv, _k, _f, _tb in chans],
                    alignment=tier,
                    license=d["license_tag"],
                    source=_page_url(bul),
                    dataset=d["dataset_name"],
                    series_id=sid,
                    domain="climate",
                    region=d["region"],
                    period_start=_iso(keys0[0], freq),
                    period_end=_iso(keys0[-1], freq),
                    meta={
                        "provider": d["provider"],
                        "bulletin": d["bulletin_name"],
                        "theme": bul.theme,
                        "reported_month": bul.ym,
                        "section_heading": sec.heading or None,
                        "section_parent": sec.parent,
                        "topic": sec.topic,
                        "period": sec.period,
                        "series_family": chans[0][3].kind,
                        "series_stride": rule.stride,
                        "series_via_fallback": chans[0][3].kind != rule.kinds[0],
                        "n_points": n,
                        "channels": [u for u, *_ in chans],
                        "figure_data_reference_period":
                            chans[0][4].meta.get("reference period", "")
                            or chans[0][3].baseline,
                        "figures_in_text": len(figs),
                        "baseline_named_in_prose": named_bases[0] if named_bases else None,
                        "states_ranking": c3ssec.states_ranking(sec.text),
                        "true_license": d["true_license"],
                    },
                )
            except ValueError as exc:
                stat["drop_invalid"] += 1
                stat[f"invalid::{exc}"] += 1
                continue
            problems = local_checks(rec, n)
            if problems:
                stat["drop_invalid"] += 1
                stat[f"local::{problems[0]}"] += 1
                continue
            records.append(rec)
            seen_text.add(text)
            seen_sid.add(sid)
            stat["emitted"] += 1
            stat[f"emit::{bul.theme}"] += 1
            if maxrec is not None and len(records) >= int(maxrec):
                break
        if maxrec is not None and len(records) >= int(maxrec):
            break
    return records, {"stats": dict(stat), "reconcile": reconcile(stat)}


def _terminal_is_reported(last_key: str, ym: str, stride: str) -> bool:
    """Does the series' last point fall in the month the bulletin reports?"""
    if stride == "daily":
        return last_key[:7] == ym
    return last_key[:7] == ym if len(last_key) >= 7 else last_key == ym[:4]


#: The quantity each family's prose is actually about, as a marker in the column names. Where
#: a bulletin publishes several files of one family, the one carrying this quantity wins.
_PREFERRED_QUANTITY = {
    "sie_calendar_month": ("sie",),        # extent, not area: the prose ranks extent
}


def _has_preferred_quantity(kind: str, tab: c3sdata.Table) -> bool:
    want = _PREFERRED_QUANTITY.get(kind)
    if not want:
        return True
    cols = " ".join(tab.cols).lower()
    return any(w in cols for w in want)


#: column name -> the region it is about, for files that hold several regions side by side
_REGION_ALIASES = {"arctic": "arctic", "antarctic": "antarctic",
                   "global": "global", "european": "europe", "europe": "europe"}

#: sea-ice figure data publishes four anomaly columns per basin (extent and area, absolute
#: and per cent); each needs its own unit or a record carries four channels with one name.
_SIE_COLS = {
    "sie anomaly": "sea_ice_extent_anomaly_mkm2",
    "sie anomaly %": "sea_ice_extent_anomaly_pct",
    "sia anomaly": "sea_ice_area_anomaly_mkm2",
    "sia anomaly %": "sea_ice_area_anomaly_pct",
}
#: a column that names a region rather than a variable: the region overrides the file's own
#: domain, because one file can hold both ("Month,Global,European" in a temperature series).
_REGION_COLS = {"global": "global", "european": "europe", "europe": "europe"}


def _unit_name(fam: c3sdata.Family, dom: str, col: str,
               tab: Optional[c3sdata.Table] = None) -> str:
    """A channel's unit. Always column-derived: the same file routinely holds several
    anomaly columns, and a name that ignores the column gives them all one unit -- which
    `validate.py --strict` rejects outright, so 358 records were being lost to it."""
    c = col.strip().lower()
    if fam.kind == "sie_calendar_month":
        return f"{dom}_{_SIE_COLS.get(c, c.replace(' ', '_').replace('%', 'pct'))}"
    if fam.kind == "sst_daily":
        return "sst_anomaly_degc_60s_60n" + ("" if "anom" in c else f"_{c}".replace(" ", "_"))
    if fam.kind in ("sat_calendar_month", "sat_allmonths", "sat_12month"):
        region = _REGION_COLS.get(c, dom)
        suffix = "_12month_mean" if fam.kind == "sat_12month" else ""
        if c == "ano_pi":
            suffix += "_preindustrial"
        return f"{region}_sat_anomaly_degc{suffix}"
    if fam.kind == "hydro_4month":
        return f"{dom}_{c3sdata.variable_of(col, tab) if tab else c}"
    var = c3sdata.variable_of(col, tab) if tab else c
    region = _REGION_COLS.get(c, dom)
    return f"{region}_{var}".lower().replace(" ", "_")


def _iso(key: str, freq: str) -> str:
    if len(key) == 4:
        return f"{key}-01-01"
    if len(key) == 7:
        return f"{key}-01"
    return key


def _page_url(bul: Bulletin) -> str:
    return c3ssrc.candidates(bul.theme, bul.ym)[0].url


def local_checks(rec: dict, n: int) -> List[str]:
    e = []
    if rec["text"].count("<ts></ts>") != 1:
        e.append("ts token count")
    lens = {len(c["values"]) for c in rec["timeseries"]}
    if lens != {n}:
        e.append(f"channel lengths {sorted(lens)} != {n}")
    if any(c["values"][-1] is None for c in rec["timeseries"]):
        e.append("terminal null")
    return e


def reconcile(stat: collections.Counter) -> dict:
    units = stat["section_units"]
    acc = (stat["emitted"] + stat["drop_short_text"] + stat["drop_no_rule"]
           + stat["drop_no_series_for_section"] + stat["drop_all_channels_unusable"]
           + stat["drop_short_window"] + stat["drop_duplicate_series_id"]
           + stat["drop_duplicate_text"] + stat["drop_invalid"])
    return {"section_units": units, "accounted": acc, "balances": units == acc}


# --- audits ---------------------------------------------------------------

def audit_alignment(cfg) -> dict:
    import random
    path = rp(cfg["output"]["output_path"])
    if not path.exists():
        return {"error": f"{path} not built"}
    recs = [json.loads(l) for l in path.open()]
    n = len(recs)
    out: Dict[str, Any] = {"records": n}
    out["structural"] = {
        "channel length == n_points":
            f"{sum(1 for r in recs if all(len(c['values']) == r['meta']['n_points'] for c in r['timeseries']))}/{n}",
        "terminal non-null":
            f"{sum(1 for r in recs if all(c['values'][-1] is not None for c in r['timeseries']))}/{n}",
        "exactly one <ts></ts>": f"{sum(1 for r in recs if r['text'].count('<ts></ts>') == 1)}/{n}",
        "series ends at or before the reported month":
            f"{sum(1 for r in recs if r['period_end'][:7] <= r['meta']['reported_month'])}/{n}",
    }
    tot = miss = clean = 0
    for r in recs:
        body = r["text"].split("\n\n<ts></ts>")[0]
        vals = [v for c in r["timeseries"] for v in c["values"] if v is not None]
        bad = sum(1 for f in c3ssec.figures(body)
                  if not any(c3ssec.quotes(f, v) for v in vals))
        got = len(c3ssec.figures(body))
        tot += got
        miss += bad
        clean += bad == 0
    out["quotation"] = {"figures_quoted": tot, "not_in_own_series": miss,
                        "pct": round(100.0 * miss / max(1, tot), 2),
                        "records_all_figures_present": f"{clean}/{n}"}
    random.seed(23)
    by = collections.defaultdict(list)
    for i, r in enumerate(recs):
        by[(r["meta"]["theme"], r["meta"]["topic"], r["meta"]["period"])].append(i)
    true = ctrl = ctrl_n = 0
    for r in recs:
        body = r["text"].split("\n\n<ts></ts>")[0]
        figs = c3ssec.figures(body)
        terms = [c["values"][-1] for c in r["timeseries"]]
        true += any(c3ssec.quotes(f, tv) for f in figs for tv in terms)
        peers = [j for j in by[(r["meta"]["theme"], r["meta"]["topic"], r["meta"]["period"])]
                 if recs[j]["meta"]["reported_month"] != r["meta"]["reported_month"]]
        if not peers:
            continue
        o = recs[random.choice(peers)]
        ot = [c["values"][-1] for c in o["timeseries"]]
        ctrl += any(c3ssec.quotes(f, tv) for f in figs for tv in ot)
        ctrl_n += 1
    out["tier"] = {"terminal_value_quoted": f"{true}/{n} = {100*true/n:.1f}%",
                   "permutation_control": f"{ctrl}/{ctrl_n} = {100*ctrl/max(1,ctrl_n):.1f}%",
                   "lift_pp": round(100*true/n - 100*ctrl/max(1, ctrl_n), 1),
                   "tags_in_file": dict(collections.Counter(r["alignment"] for r in recs))}

    # -- the exact test this package can answer: a stated rank-in-the-record, checked against
    #    the very series it is a claim about. "the sixth warmest June on record" either is the
    #    terminal point's position in its own calendar-month record or it is not.
    exact_ok = exact_n = near_ok = ambiguous = 0
    examples = []
    for r in recs:
        if r["meta"]["series_stride"] != "annual":
            continue
        body = r["text"].split("\n\n<ts></ts>")[0]
        claims = c3ssec.stated_ranks(body)
        if not claims:
            continue
        # Only sections making ONE rank claim are scored. A section that makes several
        # ("third lowest July extent ... the second lowest was 2017 ... ninth in our dataset")
        # cannot be attributed to one channel by an automated reader, and scoring them against
        # the record's single channel measures the reader, not the pairing: hand-checking the
        # 2020-10 Arctic record showed the series exactly right (terminal -2.978, matching the
        # prose's "3.0 million km2 below", ranked 1st of 42 as the prose says) while the
        # multi-claim reader scored it a miss.
        if len({c[0] for c in claims}) > 1:
            ambiguous += 1
            continue
        for want, hi in claims[:1]:
            best = None
            for c in r["timeseries"]:
                vals = [v for v in c["values"] if v is not None]
                if len(vals) < 10 or c["values"][-1] is None:
                    continue
                pos = sorted(vals, reverse=hi).index(c["values"][-1]) + 1
                if best is None or abs(pos - want) < abs(best - want):
                    best = pos
            if best is None:
                continue
            exact_n += 1
            if best == want:
                exact_ok += 1
            if abs(best - want) <= 1:
                near_ok += 1
            elif len(examples) < 6:
                examples.append({"series_id": r["series_id"], "stated": want, "actual": best})
    out["stated_rank_reproduces"] = {
        "sections_with_multiple_rank_claims_not_scored": ambiguous,
        "rank_claims_checked": exact_n,
        "exact": f"{exact_ok}/{exact_n} = {100*exact_ok/max(1,exact_n):.1f}%",
        "within_1": f"{near_ok}/{exact_n} = {100*near_ok/max(1,exact_n):.1f}%",
        "counterexamples": examples,
    }

    # -- and the sign test, which every anomaly series can answer against a 50% baseline
    sign_ok = sign_n = 0
    for r in recs:
        body = r["text"].split("\n\n<ts></ts>")[0]
        want = c3ssec.stated_anomaly_sign(body)
        if want is None:
            continue
        terms = [c["values"][-1] for c in r["timeseries"] if c["values"][-1] is not None]
        if not terms:
            continue
        sign_n += 1
        got = 1 if sum(1 for t in terms if t > 0) >= sum(1 for t in terms if t < 0) else -1
        sign_ok += got == want
    out["stated_above_or_below_average"] = {
        "records_making_the_claim": sign_n,
        "terminal_sign_agrees": f"{sign_ok}/{sign_n} = {100*sign_ok/max(1,sign_n):.1f}%",
        "chance_baseline": "50%",
    }
    lens = {len(c["values"]) for r in recs for c in r["timeseries"]}
    dp = sum(len(c["values"]) for r in recs for c in r["timeseries"])
    nulls = sum(1 for r in recs for c in r["timeseries"] for v in c["values"] if v is None)
    ptsf = collections.defaultdict(list)
    for r in recs:
        ptsf[r["meta"]["series_family"]].append(r["meta"]["n_points"])
    out["series_health"] = {
        "points_per_channel_range": [min(lens), max(lens)],
        "pct_records_ge_32_points":
            round(100.0 * sum(1 for r in recs if r["meta"]["n_points"] >= 32) / n, 1),
        "timesteps": sum(r["meta"]["n_points"] for r in recs),
        "datapoints": dp, "null_pct": round(100.0 * nulls / dp, 2),
        "by_family": {k: {"records": len(v), "median_points": statistics.median(v)}
                      for k, v in sorted(ptsf.items(), key=lambda x: -len(x[1]))},
        "channels_per_record": {
            "median": statistics.median([len(r["timeseries"]) for r in recs]),
            "max": max(len(r["timeseries"]) for r in recs)},
        "distinct_texts": len({r["text"] for r in recs}),
        "distinct_series_id": len({r["series_id"] for r in recs}),
        "reported_month_span": [min(r["meta"]["reported_month"] for r in recs),
                                max(r["meta"]["reported_month"] for r in recs)],
        "distinct_reported_months": len({r["meta"]["reported_month"] for r in recs}),
        "themes": dict(collections.Counter(r["meta"]["theme"] for r in recs)),
        "series_via_fallback_file":
            sum(1 for r in recs if r["meta"].get("series_via_fallback")),
    }
    return out


def audit_vintage(cfg) -> dict:
    """Does ERA5 change under the bulletins? Same month, as published in different bulletins.

    There is no third-party series to diff against -- the figure data *is* what the prose was
    written from, which is this package's structural advantage over `47/48/49/50`. But ERA5 is
    itself reanalysed, so the same historical month can read differently in a later bulletin.
    This measures that directly, so the package can state whether a record's series is the
    vintage its own prose was written against.
    """
    idx = load_index(cfg)
    series: Dict[Tuple[str, str, str], Dict[str, List[Tuple[str, float]]]] = \
        collections.defaultdict(lambda: collections.defaultdict(list))
    for page, names in sorted(idx.items()):
        theme, ymc = page.replace(".html", "").rsplit("_", 1)
        bym = f"{ymc[:4]}-{ymc[4:]}"
        for nm in names:
            tab = load_table(cfg, nm)
            fam = family_of(cfg, nm)
            if tab is None or fam is None or fam.kind not in ("sat_allmonths", "rh_monthly"):
                continue
            col = c3sdata.pick_columns(fam, tab)[0]
            for k, v in tab.rows.items():
                if len(k) == 7 and v.get(col) is not None:
                    series[(fam.kind, fam.domain, col)][k].append((bym, v[col]))
    same = diff = 0
    drifts: List[float] = []
    for months in series.values():
        for obs in months.values():
            if len(obs) < 2:
                continue
            vals = {round(v, 4) for _b, v in obs}
            if len(vals) == 1:
                same += 1
            else:
                diff += 1
                drifts.append(max(vals) - min(vals))
    return {"month_values_published_in_2plus_bulletins": same + diff,
            "identical_across_bulletins": same, "revised": diff,
            "pct_identical": round(100.0 * same / max(1, same + diff), 2),
            "median_abs_revision": round(statistics.median(drifts), 4) if drifts else None,
            "max_abs_revision": round(max(drifts), 4) if drifts else None}


def run(cfg, dry: bool) -> dict:
    d, o = cfg["data"], cfg["output"]
    records, report = build(cfg)
    report.update({"provider": d["provider"], "bulletin": d["bulletin_name"],
                   "dataset": d["dataset_name"], "config_snapshot": cfg, "dry_run": dry})
    if not report["reconcile"]["balances"] and o.get("max_records") is None:
        raise SystemExit(f"reconcile does not balance: {report['reconcile']}")
    if dry:
        if records:
            r0 = dict(records[0])
            r0["text"] = r0["text"][:600] + "…"
            r0["timeseries"] = [{**ts, "values": ts["values"][:5] + ["…"]}
                                for ts in r0["timeseries"][:3]]
            print(json.dumps(r0, ensure_ascii=False, indent=2)[:2600])
        print(json.dumps({k: v for k, v in report.items() if k != "config_snapshot"},
                         indent=2)[:3500])
        return report
    op = rp(o["output_path"])
    op.parent.mkdir(parents=True, exist_ok=True)
    with op.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    if records and o.get("samples_path"):
        sp = rp(o["samples_path"])
        sp.parent.mkdir(parents=True, exist_ok=True)
        with sp.open("w", encoding="utf-8") as fh:
            json.dump(records[:3], fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    rpp = rp(o["report_path"])
    rpp.parent.mkdir(parents=True, exist_ok=True)
    rpp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Build C3S Climate Bulletin → CPT JSONL")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--set", dest="set", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--audit-alignment", action="store_true")
    ap.add_argument("--audit-vintage", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    if args.audit_alignment:
        print(json.dumps(audit_alignment(cfg), indent=2))
        return
    if args.audit_vintage:
        print(json.dumps(audit_vintage(cfg), indent=2))
        return
    rep = run(cfg, dry=args.dry_run)
    s = rep["stats"]
    print(f"\nDone: {s.get('emitted', 0)} records from {s.get('section_units', 0)} section "
          f"units ({s.get('bulletins', 0)} bulletins). "
          f"reconcile={rep['reconcile']['balances']}", file=sys.stderr)


if __name__ == "__main__":
    main()
