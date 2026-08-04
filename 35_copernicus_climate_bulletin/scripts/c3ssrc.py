"""The Copernicus C3S Climate Bulletin universe: three themes, six URL slugs, one calendar.

C3S publishes the monthly bulletin as **three separate pages per month**, and the bulletin
navigation on every page names all three ("See all months for: Surface air temperature /
Sea ice cover / Hydrological variables"). The banked build read two of them; the
hydrological theme was never built at all.

Each theme's URL slug also changed at least once, so the universe is enumerated by trying
every known slug per month rather than by assuming an era boundary -- the boundary itself is
then a *measured* output (`harvest.py --report`) instead of a constant that can silently be
wrong by a year. Spans below are what the Wayback CDX enumeration found over
`climate.copernicus.eu` (1,588 dated bulletin URLs), and every slug was confirmed to still
resolve 200 on the live site:

    temperature   average-surface-air-temperatures            2015-08 .. 2017-05
                  surface-air-temperature                     2017-06 .. present
    sea_ice       sea-ice                                     2017-03 .. 2017-12
                  sea-ice-cover                               2017-04 .. present
    hydrological  precipitation-relative-humidity-and-soil-moisture          2017-04 .. 2026-05
                  precipitation-relative-humidity-soil-moisture-and-river-flow  2026-01 .. present

The earliest bulletin of any theme is 2015-08, which is 41 months before the banked build's
`start_month: 2019-01`.
"""
from __future__ import annotations

import calendar
from typing import Dict, List, NamedTuple, Tuple

BASE = "https://climate.copernicus.eu"

#: Slugs are tried in this order; the first that returns a page with real narrative wins.
#: Newest-first so the current naming is hit on one request for most of the corpus.
THEME_SLUGS: Dict[str, List[str]] = {
    "temperature": ["surface-air-temperature", "average-surface-air-temperatures"],
    "sea_ice": ["sea-ice-cover", "sea-ice"],
    "hydrological": ["precipitation-relative-humidity-soil-moisture-and-river-flow",
                     "precipitation-relative-humidity-and-soil-moisture"],
}

#: The whole bulletin calendar. Kept generous on the early side on purpose: a month with no
#: page for any slug is recorded as absent, which is a fact about the source, whereas a
#: `start_month` that is merely late is a silent loss -- the banked build lost 41 months
#: that way.
FIRST_MONTH = "2015-01"


class Page(NamedTuple):
    theme: str
    ym: str          # 'YYYY-MM' -- the month the bulletin REPORTS
    slug: str
    url: str


def months(first: str, last: str) -> List[str]:
    y0, m0 = int(first[:4]), int(first[5:7])
    y1, m1 = int(last[:4]), int(last[5:7])
    out = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def candidates(theme: str, ym: str) -> List[Page]:
    """Every URL that might hold this theme's bulletin for this month."""
    y, m = int(ym[:4]), int(ym[5:7])
    monthname = calendar.month_name[m].lower()
    return [Page(theme, ym, slug, f"{BASE}/{slug}-{monthname}-{y}")
            for slug in THEME_SLUGS[theme]]


def local_name(theme: str, ym: str) -> str:
    return f"{theme}_{ym.replace('-', '')}.html"
