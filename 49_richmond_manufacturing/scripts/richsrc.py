"""Enumerate the Richmond Fed Fifth District survey release-document universe.

The releases live in three site eras and the naming scheme changed twice, so the
universe is *listed* from the Wayback CDX index rather than guessed from a URL
template. What the eras look like (measured 2026-07-30):

  A. 1997-01 .. 2004-12  `rich.frb.org/{research/,research/regional/,}surveys/archive/`
       narrative  mfg{MM}{YY}.html          ser{MM}{YY}.html
       table      m{MM}{YY}tbl.html         stbl{MM}{YY}.html
                  mtbl{MM}{YY}.html         s{MM}{YY}tbl.html
     Two-column-free plain HTML, and the narrative carries the release's own
     section headings (Current Activity / Employment / Expectations / Prices).
     ** In this era the release published in month M reports month M-1. **

  B. 2005-01 .. 2008-09  no web capture in any layout (45 months, see harvest.py's
     report). The Bank's own 2009 archive page lists year folders 2006-2008, so the
     pages existed; Wayback holds none of the release documents.

  C. 2008-10 .. present  `richmondfed.org/...surveys_of_business_conditions/`
       {manufacturing,service_sector,services,non-manufacturing}/{YYYY}/
         pdf/{mfg,svc,nmf}_{MM}_{DD}_{YY}.pdf     <- 2-column narrative + table
         data/{mfg,svc}_busindex_{MM}_{DD}_{YY}.csv <- the table, machine-readable
         {mfg,svc}_{MM}_{DD}_{YY}[.cfm]             <- the release page (narrative)
     plus the current `-/media/RichmondFedOrg/region_communities/...` tree.
     ** In this era the release published in month M reports month M. **

The M-vs-M-1 flip inside the 2005-2007 gap is why no code here computes a data
month: `richtab` reads it off the table's own column headings and the builder
cross-checks the two sources against each other (see `harvest.py --report`).
"""
from __future__ import annotations

import re
from typing import Dict, List, NamedTuple, Optional

CDX = "https://web.archive.org/cdx/search/cdx"

# Retired-era archive directories. The same file is reachable under all three as the
# site was reorganised mid-era; any that answers 200 will do.
RETIRED_BASES = [
    "http://www.rich.frb.org/research/surveys/archive",
    "http://www.rich.frb.org/surveys/archive",
    "http://www.rich.frb.org/research/regional/surveys/archive",
]

# per-survey filename vocabulary
SURVEY_NAMES = {
    "mfg": {
        "retired_narr": [r"mfg(\d\d)(\d\d)\.html?"],
        "retired_tbl": [r"m(\d\d)(\d\d)tbl\.html?", r"mtbl(\d\d)(\d\d)\.html?",
                        r"mfg(\d\d)(\d\d)tbl\.html?"],
        "modern_prefix": ["mfg"],
    },
    "svc": {
        "retired_narr": [r"ser(\d\d)(\d\d)\.html?"],
        "retired_tbl": [r"stbl(\d\d)(\d\d)\.html?", r"s(\d\d)(\d\d)tbl\.html?"],
        "modern_prefix": ["svc", "ser", "nmf"],
    },
}


class Doc(NamedTuple):
    survey: str          # 'mfg' | 'svc'
    era: str             # 'retired' | 'modern'
    kind: str            # 'narr_html' | 'tbl_html' | 'pdf' | 'csv' | 'page'
    release: str         # 'YYYY-MM' for retired, 'YYYY-MM-DD' for modern
    url: str
    timestamp: str       # Wayback capture to replay ('' = fetch live)


def _yy_to_year(yy: int) -> int:
    return 1900 + yy if yy >= 90 else 2000 + yy


def _basename(url: str) -> str:
    return url.split("?")[0].rstrip("/").rsplit("/", 1)[-1].lower()


def classify(url: str, timestamp: str, survey: str) -> Optional[Doc]:
    """One CDX row -> a Doc, or None if the URL is not a release document."""
    names = SURVEY_NAMES[survey]
    b = _basename(url)

    for pat in names["retired_narr"]:
        m = re.fullmatch(pat, b)
        if m:
            mm, yy = int(m.group(1)), int(m.group(2))
            if not 1 <= mm <= 12:
                return None
            return Doc(survey, "retired", "narr_html",
                       f"{_yy_to_year(yy):04d}-{mm:02d}", url, timestamp)
    for pat in names["retired_tbl"]:
        m = re.fullmatch(pat, b)
        if m:
            mm, yy = int(m.group(1)), int(m.group(2))
            if not 1 <= mm <= 12:
                return None
            return Doc(survey, "retired", "tbl_html",
                       f"{_yy_to_year(yy):04d}-{mm:02d}", url, timestamp)

    pre = "|".join(names["modern_prefix"])
    m = re.fullmatch(rf"(?:{pre})_(busindex_)?(\d\d)_(\d\d)_(\d\d(?:\d\d)?)(\.pdf|\.csv|\.cfm)?", b)
    if m:
        isdata, mm, dd, yy, ext = m.groups()
        mm, dd = int(mm), int(dd)
        yr = int(yy) if len(yy) == 4 else _yy_to_year(int(yy))
        if not (1 <= mm <= 12 and 1 <= dd <= 31):
            return None
        rel = f"{yr:04d}-{mm:02d}-{dd:02d}"
        kind = "csv" if isdata else ("pdf" if ext == ".pdf" else "page")
        if isdata and ext != ".csv":
            return None
        return Doc(survey, "modern", kind, rel, url, timestamp)
    return None


def cdx_queries(survey: str) -> List[Dict[str, str]]:
    """The CDX calls that enumerate this survey. Kept explicit so the harvest report
    can say which listing produced which document."""
    names = SURVEY_NAMES[survey]
    retired = "|".join(p.replace("\\.", ".").replace("(\\d\\d)", "[0-9]{2}")
                       for p in names["retired_narr"] + names["retired_tbl"])
    pre = "|".join(names["modern_prefix"])
    return [
        {"name": "retired",
         "url": "rich.frb.org", "matchType": "domain",
         "filter": rf"original:.*/({retired})$"},
        {"name": "modern",
         "url": "richmondfed.org", "matchType": "domain",
         "filter": rf"original:.*/({pre})_[0-9]{{2}}_[0-9]{{2}}_[0-9]{{2,4}}(\.pdf|\.csv|\.cfm)?$"},
        {"name": "modern_data",
         "url": "richmondfed.org", "matchType": "domain",
         "filter": rf"original:.*/({pre})_busindex_[0-9]{{2}}_[0-9]{{2}}_[0-9]{{2,4}}\.csv$"},
    ]


def release_to_month(release: str) -> str:
    """'YYYY-MM-DD' or 'YYYY-MM' -> 'YYYY-MM' (the *release* month, not the data month)."""
    return release[:7]
