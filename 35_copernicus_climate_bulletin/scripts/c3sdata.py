"""Parse the C3S bulletin figure-data CSVs, and classify them into series families.

Every bulletin links the CSV behind each of its figures, which is what makes this package
possible: the series is the *same file the figure was drawn from*, published by C3S alongside
the prose that discusses it. There is no separate data source to reconcile and no vintage
question of the `47/48/49/50` kind -- though ERA5 *is* revised, which `--audit-vintage`
measures by comparing the same month as published in different bulletins.

Two things make a naive reader fail here, and both are handled by classifying on the
**descriptive** part of the filename rather than its shape:

  * **Figure numbers churn.** `global_allmonths` is `Fig1b` in most months but `Fig3b` in
    2024-11 and `Fig6b` in 2026-05; Europe moves between `Fig3b`, `Fig4b` and `Fig6b`. Any
    matcher keyed on the figure number silently loses whole eras.
  * **Header layout churns.** Some files carry a `# Description:`/`# Columns:` comment block,
    others a bare `Col. 1: last month ...` prose preamble, and the date column is variously
    `YYYY-MM`, `YYYYMM` or `YYYY-MM-DD`. So the header row is *found* (the last comma-bearing
    line before the first line whose first field parses as a date) rather than assumed.
"""
from __future__ import annotations

import csv
import io
import math
import re
from typing import Dict, List, NamedTuple, Optional, Tuple

_DATE_PATS = (
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), "1d"),
    (re.compile(r"^(\d{4})-(\d{2})$"), "1m"),
    (re.compile(r"^(\d{4})(\d{2})$"), "1m"),
    (re.compile(r"^(\d{4})$"), "1y"),
)


def _date_key(tok: str) -> Optional[Tuple[str, str]]:
    t = tok.strip().strip('"')
    for pat, freq in _DATE_PATS:
        m = pat.match(t)
        if not m:
            continue
        g = m.groups()
        if freq == "1d":
            return f"{g[0]}-{g[1]}-{g[2]}", freq
        if freq == "1m":
            if not 1 <= int(g[1]) <= 12:
                return None
            return f"{g[0]}-{g[1]}", freq
        return g[0], freq
    return None


class Table(NamedTuple):
    cols: List[str]                       # value column names, in file order
    rows: Dict[str, Dict[str, Optional[float]]]   # date key -> {col: value}
    freq: str                             # '1d' | '1m' | '1y'
    meta: Dict[str, str]                  # description / reference period / region, verbatim


def parse(raw: bytes) -> Optional[Table]:
    text = raw.decode("utf8", "ignore")
    if text.lstrip()[:200].lower().startswith("<!doctype"):
        return None                       # the site's 404 shell, served at status 200
    lines = text.splitlines()
    meta: Dict[str, str] = {}
    preamble: List[str] = []
    for ln in lines[:25]:
        s = ln.lstrip("# ").strip()
        m = re.match(r"(?i)(description|data source|reference period|method|units|"
                     r"last updated|status|data availability)\s*:\s*(.+)", s)
        if m:
            meta.setdefault(m.group(1).lower(), m.group(2).strip())
        elif s:
            head = s.split(",")[0].split()
            if not _date_key(head[0] if head else ""):
                preamble.append(s)
    # One era's figure data has an opaque filename and a colon-free title line
    # ("Monthly ERA-Interim 2m relative humidity anomalies (%) over land relative to
    # 1981-2010"), so the preamble is retained for `classify_content` to read.
    if preamble:
        meta.setdefault("preamble", " | ".join(preamble[:4]))

    # Delimiter is detected, not assumed: some early-era figure data is published as a
    # fixed-width/whitespace table ("197901   -0.3283    0.2263") rather than CSV, and a
    # comma-only reader returns nothing for it -- silently, since the file still exists.
    def split(ln: str) -> List[str]:
        return ln.split(",") if "," in ln else ln.split()

    first_data = None
    for i, ln in enumerate(lines):
        parts = split(ln)
        if len(parts) >= 2 and _date_key(parts[0]):
            first_data = i
            break
    if first_data is None:
        return None
    header: List[str] = []
    for j in range(first_data - 1, -1, -1):
        cand = split(lines[j])
        if len(cand) >= 2 and not _date_key(cand[0]):
            header = [c.strip().strip('"') for c in cand]
            break
    rows: Dict[str, Dict[str, Optional[float]]] = {}
    freq = "1m"
    ncol = 0
    for ln in lines[first_data:]:
        parts = (next(csv.reader(io.StringIO(ln)), None) if "," in ln else split(ln))
        if not parts:
            continue
        # One era keys a multi-month mean by its period range ("197901 to 197904   0.3378")
        # rather than by its last month. Read the *end* of the range, matching the convention
        # the modern files state explicitly ("Col. 1: last month of the time period"). Before
        # this, the row parsed "successfully" with `to` and the end-month as values -- a
        # silent wrong parse, which is worse than a failure.
        if len(parts) >= 4 and parts[1].strip().lower() == "to" and _date_key(parts[2]):
            dk = _date_key(parts[2])
            parts = [parts[2]] + parts[3:]
        else:
            dk = _date_key(parts[0])
        if not dk:
            continue
        key, freq = dk
        vals: Dict[str, Optional[float]] = {}
        for idx, cell in enumerate(parts[1:], start=1):
            name = header[idx] if idx < len(header) else f"col{idx}"
            c = cell.strip()
            try:
                f = float(c)
                # `float("nan")` and `float("inf")` SUCCEED, so a literal `nan` cell -- which the
                # C3S sea-ice and hydrological CSVs use for a missing month -- never reached the
                # ValueError branch below and was stored as a float NaN. That NaN then bypassed
                # every downstream gate here, all of which test `is None`: max_null_fraction
                # counted it as present, and the trailing-point checks accepted it. It ended up in
                # 508 values across 98 shipped records, where it serialises as the bare token
                # `NaN` -- not valid JSON (RFC 8259) -- and crashed the team's verify_cpt.py.
                # A non-finite cell means "no measurement", which is exactly None here.
                vals[name] = f if math.isfinite(f) else None
            except ValueError:
                vals[name] = None
        ncol = max(ncol, len(vals))
        rows[key] = vals
    if not rows:
        return None
    # Frequency comes from the *set* of keys, not from one literal's shape. The 2024-11
    # onward `allmonths` files stamp monthly data as `1940-01-01`, which a shape-only reader
    # labels daily -- 40 files were being published with freq "1d" for a monthly series. If
    # every key falls on the first of a month, the series is monthly and the keys are
    # normalised to `YYYY-MM` so they join with the other monthly families.
    keys = list(rows)
    if all(len(k) == 10 for k in keys):
        if all(k.endswith("-01") for k in keys):
            rows = {k[:7]: v for k, v in rows.items()}
            freq = "1m"
        else:
            freq = "1d"
    elif all(len(k) == 7 for k in keys):
        freq = "1m"
    elif all(len(k) == 4 for k in keys):
        freq = "1y"
    cols = [c for c in (header[1:] if len(header) > 1 else [])] or \
           [f"col{i}" for i in range(1, ncol + 1)]
    return Table(cols, rows, freq, meta)


# --- family classification -------------------------------------------------

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]


class Family(NamedTuple):
    kind: str        # 'sat_calendar_month' | 'sat_allmonths' | 'sat_12month' | 'sst_daily'
                     # | 'sie_calendar_month' | 'rh_monthly' | 'hydro_4month'
    domain: str      # 'global' | 'europe' | 'arctic' | 'antarctic' | 'nweurope' | ...
    baseline: str    # '1991-2020' | '1981-2010' | '1850-1900' | ''
    calendar_month: Optional[int]     # for calendar-month series


_BASELINE_RE = re.compile(r"\b(1991[-–]2020|1981[-–]2010|1850[-–]1900)\b")


def baseline_of(tab: "Table") -> str:
    """The reference period the file itself states, or '' if it does not.

    Never defaulted: the older sea-ice files carry no baseline in their filename, and assuming
    1991-2020 for them mislabelled a 1981-2010 series. Since Antarctic sea ice declined, the
    same month is a *negative* anomaly against 1981-2010 and a *positive* one against
    1991-2020 -- so the wrong label silently inverted the sign relative to the prose.
    """
    blob = " ".join(list(tab.meta.values()))
    m = _BASELINE_RE.search(blob)
    return m.group(1).replace("–", "-") if m else ""


def classify(name: str) -> Optional[Family]:
    """Filename -> series family. Matched on the descriptive part, never the figure number."""
    n = name.lower()
    base = ""
    for b in ("1991-2020", "1981-2010", "1850-1900"):
        if b in n:
            base = b
            break

    def month_in(s: str) -> Optional[int]:
        for i, m in enumerate(MONTHS, start=1):
            if f"_{m}_" in s or f"_{m}." in s or f"_{m}_data" in s:
                return i
        return None

    # -- sea ice: monthly extent anomalies for one calendar month across years
    if "monthly_extent_anomalies" in n or ("osi-saf" in n and "sie" in n):
        # "antarctic" contains "arctic", so the Antarctic must be tested first -- testing
        # for "arctic" first classified all 112 Antarctic files as Arctic and the Antarctic
        # channel silently vanished from the corpus.
        dom = "antarctic" if "antarctic" in n else ("arctic" if "arctic" in n else "")
        if not dom:
            return None
        return Family("sie_calendar_month", dom, base, month_in(n))

    # -- hydrological
    if "4month_anomaly" in n and "hydro" in n:
        for reg in ("nweurope", "neeurope", "sweurope", "seeurope"):
            if reg in n:
                return Family("hydro_4month", reg, base, None)
        return None
    if re.search(r"_r2_|_r2\.|_R2_", name) or "timeseries_rh" in n or "_rh_anomal" in n:
        return Family("rh_monthly", "global_and_europe", base, None)

    # -- sea surface temperature (daily)
    if "daily_sst" in n or ("sst" in n and "daily" in n):
        return Family("sst_daily", "60s-60n", base, None)

    # -- surface air temperature
    dom = "europe" if "europe" in n else ("global" if "global" in n else "")
    if dom:
        if "12month" in n:
            return Family("sat_12month", dom, base, None)
        if "allmonths" in n:
            return Family("sat_allmonths", dom, base, None)
        cm = month_in(n)
        if cm:
            return Family("sat_calendar_month", dom, base, cm)
        if "1month_anomaly" in n:
            return Family("sat_allmonths", dom, base, None)
    return None


#: Content-based fallback for opaque filenames. One era publishes figure data as
#: `2018-09_Data_for_month_10_2017_plot_5.csv`, which says nothing about what it holds -- but
#: the file's own first line does ("Monthly ERA-Interim 2m relative humidity anomalies (%)
#: over land relative to 1981-2010"). Classifying on the payload rather than the name is the
#: same rule that settled the `50_richmond_nonmanufacturing` workbook question.
_CONTENT_KIND: List[Tuple[re.Pattern, str, str]] = [
    (re.compile(r"(?i)precipitation rate anomal"), "hydro_4month", ""),
    (re.compile(r"(?i)relative humidity"), "rh_monthly", "global_and_europe"),
    (re.compile(r"(?i)sea ice (extent|area)"), "sie_calendar_month", ""),
    (re.compile(r"(?i)sea surface temperature"), "sst_daily", "60s-60n"),
    (re.compile(r"(?i)hydrological"), "hydro_4month", ""),
    (re.compile(r"(?i)(2m |surface air )?temperature anomal"), "sat_allmonths", ""),
]


def classify_content(tab: "Table", name: str = "") -> Optional[Family]:
    blob = " ".join([tab.meta.get("description", "")] + list(tab.meta.values()))
    cols = " ".join(tab.cols).lower()
    for pat, kind, dom in _CONTENT_KIND:
        if pat.search(blob):
            if not dom and kind == "hydro_4month":
                # the region is in the column header ("NW Europe"), not the filename
                for tag, reg in (("nw", "nweurope"), ("ne", "neeurope"),
                                 ("sw", "sweurope"), ("se", "seeurope")):
                    if re.search(rf"(?i)\b{tag}\b", cols) or re.search(rf"(?i)\b{tag} europe", blob):
                        dom = reg
                        break
            if not dom:
                if "arctic" in blob.lower():
                    dom = "antarctic" if "antarctic" in blob.lower() else "arctic"
                elif "europe" in cols and "global" in cols:
                    dom = "global_and_europe"
                elif "europe" in blob.lower():
                    dom = "europe"
                else:
                    dom = "global"
            base = "1981-2010" if "1981-2010" in blob else (
                "1991-2020" if "1991-2020" in blob else "")
            return Family(kind, dom, base, None)
    return None


#: Which value column of a family's table is the anomaly to publish, in preference order.
#: Absolute temperature and the climatology are deliberately not used: the prose is written
#: entirely in anomalies ("0.24°C above the 1991-2020 average"), so an absolute-value channel
#: would sit beside prose that never quotes it.
#: Column names are matched **case-insensitively and across naming eras**. C3S renames these
#: without notice -- the relative-humidity files head their columns `Global`/`European` in one
#: era and `global`/`European` in another, and the hydrological files use ERA5 short names
#: `tp`/`swvl1`/`2t`/`r2` in one era and `MTPR`/`SWVL1`/`T2`/`R2` in another. An exact-match
#: reader silently published one channel where the file held two, and fell through to a
#: generic fallback that named four distinct variables identically.
ANOMALY_COLS: Dict[str, List[str]] = {
    "sat_calendar_month": ["ano_91-20", "ano_pi", "anomaly"],
    "sat_allmonths": ["ano_91-20", "ano_pi", "anomaly"],
    "sat_12month": ["ano_91-20", "anomaly"],
    "sst_daily": ["anomaly", "ano_91-20", "sst_anomaly"],
    "sie_calendar_month": ["anomaly", "extent_anomaly"],
    "rh_monthly": ["global", "european"],
    "hydro_4month": ["tp", "mtpr", "swvl1", "2t", "t2", "r2"],
}

#: Human-readable channel unit names, by (family kind, column).
CHANNEL_UNITS: Dict[Tuple[str, str], str] = {
    ("rh_monthly", "global"): "global_relative_humidity_anomaly",
    ("rh_monthly", "european"): "europe_relative_humidity_anomaly",
}

#: ERA5 short names used as column headers in the hydrological figure data.
_VARIABLE_CODES = {
    "tp": "precipitation_anomaly", "mtpr": "precipitation_anomaly",
    "swvl1": "soil_moisture_anomaly",
    "2t": "sat_anomaly", "t2": "sat_anomaly",
    "r2": "relative_humidity_anomaly",
    "global": "global_relative_humidity_anomaly",
    "european": "europe_relative_humidity_anomaly",
}

#: When the column header is not a variable code, the variable is in the file's own title
#: line. One era heads its regional columns "NW Europe" -- whitespace-split, that yields a
#: column literally named `NW`, and naming a channel `nweurope_NW` ships a series whose unit
#: says nothing about what it measures. The title line always does say.
_VARIABLE_FROM_TEXT = [
    (re.compile(r"(?i)precipitation"), "precipitation_anomaly"),
    (re.compile(r"(?i)soil moisture|volumetric moisture"), "soil_moisture_anomaly"),
    (re.compile(r"(?i)relative humidity"), "relative_humidity_anomaly"),
    (re.compile(r"(?i)sea ice (extent|area)"), "sea_ice_extent_anomaly"),
    (re.compile(r"(?i)sea surface temperature"), "sst_anomaly"),
    (re.compile(r"(?i)temperature"), "sat_anomaly"),
]


def variable_of(col: str, tab: "Table") -> str:
    """What a column measures: its ERA5 code if it is one, else the file's own title line."""
    code = _VARIABLE_CODES.get(col.strip().lower())
    if code:
        return code
    blob = " ".join(tab.meta.values())
    for pat, name in _VARIABLE_FROM_TEXT:
        if pat.search(blob):
            return name
    return re.sub(r"[^a-z0-9]+", "_", col.strip().lower()) or "anomaly"


def pick_columns(fam: Family, tab: Table) -> List[str]:
    """The value columns to publish for this family, in the file's own order."""
    want = {w.lower() for w in ANOMALY_COLS.get(fam.kind, [])}
    have = [c for c in tab.cols if c.strip().lower() in want]
    if have:
        return have
    # Fall back to every numeric column that is not an absolute value or a climatology.
    # `2t`/`t2` are absolutes for the temperature families but are the *temperature anomaly*
    # channel of the hydrological files, so the exclusion is family-aware.
    drop = r"sst|clim.*|offset.*|month|period"
    if not fam.kind.startswith("hydro"):
        drop += r"|2t|t2"
    return [c for c in tab.cols if not re.fullmatch(drop, c.strip(), re.I)] or tab.cols[:1]
