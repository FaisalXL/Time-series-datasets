"""Per-state fetch/extraction logic for USDA/NASS state-level "Crop Progress and Condition"
weekly reports.

Every state runs its own NASS field office and its own idiosyncratic archive: different base
folders, different filename conventions across 2-4 naming eras, different clean-text depth.
There is no single template that works across states (confirmed by scouting 7 states before
this build: Iowa/Kansas/Minnesota/Indiana clean back to 1997-2007; Nebraska/Illinois have real
files even older but pre-2003/2018 respectively are scanned-image or OCR-garbled and are
explicitly OUT for this build per Faisal's "clean-text only" call).

Strategy used for every era, instead of guessing each state's week-numbering scheme:
  1. Enumerate real candidate PDF URLs for a given (state, year) either directly (recent years,
     where the live site or Wayback encodes the exact report date in the filename) or via a
     Wayback CDX prefix listing of that year's folder (older eras, where files are named by an
     internal week-number we don't need to solve).
  2. Fetch each candidate, extract full text, and parse the report's OWN embedded
     "week ending <date>" line to get the ground-truth date.
  3. Join that date against the real WEEK_ENDING dates already present in the NASS Quick Stats
     bulk series (loaded separately) -- never trust the filename's date/number alone.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

import pdfplumber
import io

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

CDX_API = "https://web.archive.org/cdx/search/cdx"


def _http_get(url: str, timeout: int = 15, retries: int = 3) -> Optional[bytes]:
    """Retries default to 3 (up from 1): live scouting of 5 sibling states all independently
    hit transient web.archive.org connection failures/503s under normal sequential use (not
    specific to any one state), confirmed recoverable on retry with backoff -- without this, a
    real year's archive silently looks empty and gets skipped."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return resp.read()
                return None
        except urllib.error.HTTPError:
            return None
        except Exception:
            if attempt == retries:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def cdx_list_year_pdfs(prefix: str, year: int, year_folder_fmt: str = "{year}") -> list[tuple[str, str]]:
    """Query Wayback CDX for real (status-200) PDF captures under prefix/{year_folder}/.

    Most states file each year's reports under a plain 4-digit `{year}/` folder (the default).
    Kentucky is a confirmed exception: its real archive nests PDFs under 2-digit `cw{yy}/`
    subfolders (e.g. `cw05/cw0328.pdf`) -- a `{year}/`-shaped query returns zero hits for it
    even though a real, clean archive exists back to 2005. `year_folder_fmt` lets a
    `StateConfig` override the folder-naming scheme per state without changing this shared
    discovery logic for everyone else.

    Returns a de-duplicated list of (timestamp, original_url), one per unique URL (the most
    recent capture of that URL). CDX captures a URL multiple times across re-crawls; a URL that
    was ever a genuine 200 is worth trying even if a later crawl saw it go stale, so we keep the
    *first* 200 timestamp seen for that URL (closest to when the page was live).
    """
    folder = year_folder_fmt.format(year=year, yy=year % 100)
    q = (f"{CDX_API}?url={prefix}/{folder}/&matchType=prefix&output=json"
         f"&filter=statuscode:200&collapse=urlkey&limit=5000")
    raw = _http_get(q, timeout=15)
    if not raw:
        return []
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return []
    rows = data[1:] if data else []
    out = []
    for row in rows:
        # CDX json field order matches the query's own header row; original col index 2.
        ts, url = row[1], row[2]
        if url.lower().endswith(".pdf"):
            out.append((ts, url))
    return out


def fetch_wayback_raw(timestamp: str, original_url: str) -> Optional[bytes]:
    """Fetch the archived byte-identical content (id_ modifier = no Wayback toolbar injection)."""
    url = f"https://web.archive.org/web/{timestamp}id_/{original_url}"
    return _http_get(url, timeout=15)


def fetch_live(url: str) -> Optional[bytes]:
    return _http_get(url, timeout=30)


_WEEK_ENDING_RE = re.compile(
    r"week ending:?\s+([A-Za-z]+\.?\s+\d{1,2},?\s+\d{4})", re.IGNORECASE
)
# Some states (e.g. Ohio) only give the year via a separate numeric "Week Ending MM/DD/YY"
# table line -- the narrative's own "week ending May 21" sentence omits the year entirely.
_WEEK_ENDING_NUMERIC_RE = re.compile(
    r"week ending:?\s+(\d{1,2})/(\d{1,2})/(\d{2,4})", re.IGNORECASE
)
# Indiana's oldest era (~1997-2000) states the date bare, top-of-document, with no "week
# ending"/"released:" keyword anywhere nearby (e.g. "August 7, 2000\nReleased: Monday, 3PM" --
# "Released:" is followed by a day-of-week + time, not a date). Restricted to the first few
# lines only, to avoid matching an unrelated date mentioned later in the body prose.
_BARE_DATE_RE = re.compile(r"([A-Za-z]+\.?\s+\d{1,2},?\s+\d{4})")
_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["", "January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"]
    )
    if m
}


def parse_week_ending(text: str) -> Optional[dt.date]:
    m = _WEEK_ENDING_RE.search(text)
    if m:
        raw = m.group(1).replace(",", "").strip()
        parts = raw.split()
        if len(parts) == 3:
            mon_raw, day_raw, year_raw = parts
            mon = _MONTHS.get(mon_raw.rstrip(".").lower())
            if mon:
                try:
                    return dt.date(int(year_raw), mon, int(day_raw))
                except ValueError:
                    pass

    m2 = _WEEK_ENDING_NUMERIC_RE.search(text)
    if m2:
        mm, dd, yy = m2.groups()
        year = int(yy)
        if year < 100:
            year += 2000 if year < 70 else 1900
        try:
            return dt.date(year, int(mm), int(dd))
        except ValueError:
            pass

    m3 = _BARE_DATE_RE.search(text[:200])
    if m3:
        raw = m3.group(1).replace(",", "").strip()
        parts = raw.split()
        if len(parts) == 3:
            mon_raw, day_raw, year_raw = parts
            mon = _MONTHS.get(mon_raw.rstrip(".").lower())
            if mon:
                try:
                    return dt.date(int(year_raw), mon, int(day_raw))
                except ValueError:
                    pass
    return None


def _reconstruct_column(words: list) -> str:
    lines: dict[int, list] = {}
    for w in words:
        key = round(w["top"] / 3)
        lines.setdefault(key, []).append(w)
    out = []
    for k in sorted(lines):
        row = sorted(lines[k], key=lambda w: w["x0"])
        out.append(" ".join(w["text"] for w in row))
    return "\n".join(out)


def _has_consistent_gutter_width(words: list, best_x: float) -> bool:
    """A genuine printed two-column gutter is a fixed physical margin: its width is nearly
    constant across the dozens of rows that span it (confirmed on a real 2003 Iowa two-column
    page: ~46% of rows land within +-3pt of the median gap width, e.g. a tight 15-18pt cluster).
    A false-positive gutter -- a low-crossing x found in ordinary justified single-column text,
    where the "gap" at that x is just whatever word-boundary happens to fall there per line --
    has no such consistency (confirmed on a 2024 Kentucky single-column page real two-column
    detection would have corrupted: 0/34 rows within +-3pt of the median, gaps scattered
    2-360pt). Requiring consistency (not just a low crossing ratio) distinguishes the two.
    """
    rows: dict[int, list] = {}
    for w in words:
        rows.setdefault(round(w["top"] / 3), []).append(w)
    gaps = []
    for row_words in rows.values():
        left_x1s = [w["x1"] for w in row_words if w["x1"] <= best_x]
        right_x0s = [w["x0"] for w in row_words if w["x0"] >= best_x]
        if left_x1s and right_x0s:
            gaps.append(min(right_x0s) - max(left_x1s))
    if len(gaps) < 10:
        return False  # too few rows straddle the gutter to trust it's a real column boundary
    gaps.sort()
    median = gaps[len(gaps) // 2]
    frac_consistent = sum(1 for g in gaps if abs(g - median) <= 3) / len(gaps)
    return frac_consistent >= 0.35


def _extract_page_text(page) -> str:
    """Older-era state reports (roughly pre-2015) are typeset in two newspaper-style columns;
    pdfplumber's default reading order sorts strictly by row position, which INTERLEAVES the
    two columns' text mid-sentence for these ("Corn Planting Nearing Completion increased from
    19 percent complete..." -- headline of column A glued to column B's first sentence).

    Detect a real column gutter by scanning candidate vertical split lines in the page's middle
    band and finding the one crossed by the fewest word bounding boxes. A genuine two-column
    page has a near-zero-crossing gutter (confirmed empirically: <2% of words straddle it,
    ~balanced word counts each side) AND a consistent gutter width across rows (see
    `_has_consistent_gutter_width` -- added after Missouri and Kentucky scouting both surfaced
    the same false positive: an ordinary single-column justified page can coincidentally have a
    low-crossing x too, which without the consistency check gets mis-split into two "columns"
    that read as broken sentence fragments); a single-column page has no such gutter (every
    candidate line is crossed by many words, or the low-crossing one isn't a consistent width)
    and falls back to plain top-to-bottom extraction.
    """
    w = page.width
    words_for_detection = [wd for wd in page.extract_words() if wd["top"] > 100]
    if len(words_for_detection) < 20:
        return page.extract_text() or ""

    best_x, best_crossing = None, len(words_for_detection) + 1
    for i in range(300, 700, 4):
        x = i / 1000 * w
        crossing = sum(1 for wd in words_for_detection if wd["x0"] < x < wd["x1"])
        if crossing < best_crossing:
            best_x, best_crossing = x, crossing

    if best_crossing / len(words_for_detection) >= 0.02:
        return page.extract_text() or ""  # no clean gutter found -> single column
    if not _has_consistent_gutter_width(words_for_detection, best_x):
        return page.extract_text() or ""  # low-crossing x isn't a real fixed-width gutter

    # Exclude the masthead band (letterhead/address/title, top <= 110) from the column
    # reconstruction entirely -- it spans both columns' width and, if left in, gets shuffled
    # into whichever column its centered words happen to land in, corrupting both.
    left = [w for w in words_for_detection if (w["x0"] + w["x1"]) / 2 < best_x]
    right = [w for w in words_for_detection if (w["x0"] + w["x1"]) / 2 >= best_x]
    return _reconstruct_column(left) + "\n\n" + _reconstruct_column(right)


def pdf_to_pages_text(pdf_bytes: bytes) -> list[str]:
    pages = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                pages.append(_extract_page_text(page))
    except Exception:
        return []
    return pages


_DIGIT_RUN_RE = re.compile(r"[0-9]")
_TABLE_HEADER_MARKERS = (
    "STATION", "PRECIPITATION", "TEMPERATURE", "WEATHER SUMMARY FOR",
    "PREPARED BY AWIS", "DFN TOTAL", "GDD BASE",
)
_STATION_ROW_RE = re.compile(r"^[A-Z][a-zA-Z.'\s]{2,25}\s+\d{1,3}(\s+\d{1,3})?$")
_MASTHEAD_RE = re.compile(
    r"(nass\.usda\.gov|@nass|usda\.gov/nass|P\.?O\.?\s*Box|\bRoom\s+\d|\bRm\.?\s+\d|"
    r"\(\d{3}\)\s*\d{3}|^\d{3}-\d{3}-\d{4}|\d{3}-\d{3}-\d{4}|FAX\s|Released:?\s|Issue\s*No|"
    r"^United States Department|^National Agricultural Statistics|"
    r"^U\.?S\.?\s+Department of Agriculture|Department of Agriculture\s*$|"
    r"^AGRICULTURAL\s*$|^STATISTICS\s*$|^SERVICE\b|^KANSAS\s*$|Fact Finders|"
    r"Cooperating with|Field Office|^To access NASS|"
    r"^\d+\s+\w[\w.'\s]*\s+(?:Street|St\.?|Ste\.?|Suite|Blvd\.?|Mall|Walnut)|"
    r"^Mall\s+\w|,\s*[A-Z]{2}\s+\d{5}(-\d{4})?\b)",
    re.IGNORECASE,
)


def _is_tabular_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    digits = len(_DIGIT_RUN_RE.findall(stripped))
    letters = sum(c.isalpha() for c in stripped)
    total = max(len(stripped.replace(" ", "")), 1)
    if digits / total > 0.30 and letters < 20:
        return True
    upper_marker_hit = any(marker in stripped.upper() for marker in _TABLE_HEADER_MARKERS)
    if upper_marker_hit:
        return True
    # A weather-station row: "Station Name <temp>" or "Station Name <hi> <lo>" -- Titlecase
    # words followed by only 1-2 short numbers, no other prose content.
    return bool(_STATION_ROW_RE.match(stripped))


def _is_masthead_line(line: str) -> bool:
    return bool(_MASTHEAD_RE.search(line.strip()))


_TABLE_HEADER_WORDS_RE = re.compile(
    r"^\s*(Item|Districts?|State\b|Percent(\s+Percent)+|Days(\s+Days)+|HI\s*LO?\s*$|"
    r"Last\s+Last|Week\s+Year(\s+mal)?|Very\s*$|Poor\b.*Fair.*Good.*Excellent|"
    r"NW\s+NC\s+NE|WC\s+C\s+EC|SW\s+SC\s+SE|NA\s*=\s*[Nn]ot)",
)
_REVERSED_MARKERS = ("POSTAGE", "PERIODICALS", "POSTMASTER", "NEWSPAPER", "SUBSCRIPTION",
                     "MAILING OFFICE", "ADDITIONAL", "WALNUT", "ISSN", "PUBLISHED WEEKLY",
                     "SIOUX FALLS", "DES MOINES")
_ATTRIBUTION_RE = re.compile(
    r"^\s*(Fahrenheit\.?\s*Copyright|Copyright\s+\d{4}|[A-Z]+,?\s*Inc\.?\s*All Rights Reserved|"
    r"All Rights Reserved\.?\s*$|Degrees Fahrenheit\.?\s*Copyright)",
)


def _looks_reversed_boilerplate(line: str) -> bool:
    rev = line[::-1].upper()
    return any(marker in rev for marker in _REVERSED_MARKERS)


def clean_narrative(pages: list[str]) -> str:
    """Keep real prose paragraphs; drop the letterhead/masthead, per-station raw weather-data
    table pages/lines, bare table-header rows, and a mailing-permit stamp that some older PDFs
    render character-reversed (a rotated postal stamp, not real narrative).

    Heuristic (mirrors the digit-density approach already used for #49/#50 Richmond's
    chart-heavy PDFs): a station-data row is dense with digits and short on words; narrative
    sentences are the opposite.
    """
    full_text = "\n".join(pages)
    lines = full_text.splitlines()
    kept = [
        ln for ln in lines
        if not _is_tabular_line(ln)
        and not _is_masthead_line(ln)
        and not _TABLE_HEADER_WORDS_RE.search(ln)
        and not _looks_reversed_boilerplate(ln)
        and not _ATTRIBUTION_RE.search(ln)
    ]
    text = "\n".join(kept)
    # Collapse excess blank lines from the line-level filtering above.
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


@dataclass
class Channel:
    short_desc: str   # exact NASS SHORT_DESC key, e.g. "CORN - PROGRESS, MEASURED IN PCT PLANTED"
    unit: str         # our channel unit label, e.g. "corn_pct_planted"


@dataclass
class StateConfig:
    alpha: str
    name: str
    commodity_label: str     # human label, e.g. "corn" (used in text/meta only)
    channels: list[Channel]  # ordered; PROGRESS stages first, then condition/fieldwork extras
    clean_text_start_year: int  # earliest year confirmed clean born-digital text (scouting)
    base_prefixes: list = field(default_factory=list)  # candidate Wayback CDX folder prefixes
    year_folder_fmt: str = "{year}"  # per-year folder naming scheme; Kentucky uses "cw{yy}"

    def discover_year(self, year: int) -> list[tuple[str, str]]:
        """Every state's whole archive (all naming eras) is discoverable the same way: list
        every real (status-200) PDF Wayback ever captured under this state's hub folder(s) for
        the given year, then let the caller fetch+parse each one's own embedded date -- no need
        to solve each state's internal filename/week-numbering scheme. Several candidate base
        folders are checked because states don't agree on one: Iowa's modern (2018+) reports
        moved to a shared "Crop_Report" folder while its own 2003-2017 reports (and Illinois's
        current reports) stay under "Crop_Progress_&_Condition"; Michigan/Kentucky's *live* site
        (but not, so far, their Wayback archive) uses "State_Crop_Progress_and_Condition"."""
        hits: list[tuple[str, str]] = []
        for prefix in self.base_prefixes:
            hits.extend(cdx_list_year_pdfs(prefix, year, self.year_folder_fmt))
        return hits


def _hubs(state_folder: str) -> list:
    """Four real folder-naming variants have been observed across states/years -- e.g. Kansas
    used "Crop_Progress_&_Condition" 2001-2006 then switched to "Crop_Progress_and_Condition"
    2007-2025 (its real archive is almost entirely the second spelling); Iowa's modern era moved
    to a shared "Crop_Report" folder; Michigan and Kentucky's live (not yet Wayback-archived)
    site uses "State_Crop_Progress_and_Condition". Checking all four per state is cheap (a few
    extra CDX queries) and avoids silently missing a state's real archive because it picked one
    spelling."""
    base = f"nass.usda.gov/Statistics_by_State/{state_folder}/Publications"
    return [
        f"{base}/Crop_Progress_%26_Condition",
        f"{base}/Crop_Progress_and_Condition",
        f"{base}/Crop_Report",
        f"{base}/State_Crop_Progress_and_Condition",
    ]


def _corn_channels() -> list[Channel]:
    stages = ["PLANTED", "EMERGED", "SILKING", "DOUGH", "DENTED", "MATURE"]
    chans = [Channel(f"CORN - PROGRESS, MEASURED IN PCT {s}", f"corn_pct_{s.lower()}") for s in stages]
    chans.append(Channel("CORN, GRAIN - PROGRESS, MEASURED IN PCT HARVESTED", "corn_pct_harvested"))
    chans.append(Channel("CORN - CONDITION, MEASURED IN PCT GOOD", "corn_condition_pct_good"))
    chans.append(Channel("CORN - CONDITION, MEASURED IN PCT EXCELLENT", "corn_condition_pct_excellent"))
    chans.append(Channel("FIELDWORK - DAYS SUITABLE, MEASURED IN DAYS / WEEK", "days_suitable_per_week"))
    return chans


def _wheat_channels() -> list[Channel]:
    stages = ["PLANTED", "EMERGED", "JOINTING", "HEADED", "COLORING", "MATURE", "HARVESTED"]
    chans = [Channel(f"WHEAT, WINTER - PROGRESS, MEASURED IN PCT {s}", f"winter_wheat_pct_{s.lower()}")
             for s in stages]
    chans.append(Channel("WHEAT, WINTER - CONDITION, MEASURED IN PCT GOOD", "winter_wheat_condition_pct_good"))
    chans.append(Channel("WHEAT, WINTER - CONDITION, MEASURED IN PCT EXCELLENT",
                          "winter_wheat_condition_pct_excellent"))
    chans.append(Channel("FIELDWORK - DAYS SUITABLE, MEASURED IN DAYS / WEEK", "days_suitable_per_week"))
    return chans


STATE_CONFIGS: dict[str, StateConfig] = {
    "IA": StateConfig(alpha="IA", name="Iowa", commodity_label="corn",
                       channels=_corn_channels(), clean_text_start_year=2003, base_prefixes=_hubs("Iowa")),
    "KS": StateConfig(alpha="KS", name="Kansas", commodity_label="winter wheat",
                       channels=_wheat_channels(), clean_text_start_year=2003, base_prefixes=_hubs("Kansas")),
    "MN": StateConfig(alpha="MN", name="Minnesota", commodity_label="corn",
                       channels=_corn_channels(), clean_text_start_year=2007, base_prefixes=_hubs("Minnesota")),
    "IN": StateConfig(alpha="IN", name="Indiana", commodity_label="corn",
                       channels=_corn_channels(), clean_text_start_year=1997, base_prefixes=_hubs("Indiana")),
    "NE": StateConfig(alpha="NE", name="Nebraska", commodity_label="corn",
                       channels=_corn_channels(), clean_text_start_year=2003, base_prefixes=_hubs("Nebraska")),
    "IL": StateConfig(alpha="IL", name="Illinois", commodity_label="corn",
                       channels=_corn_channels(), clean_text_start_year=2018, base_prefixes=_hubs("Illinois")),
    "OH": StateConfig(alpha="OH", name="Ohio", commodity_label="corn",
                       channels=_corn_channels(), clean_text_start_year=2015, base_prefixes=_hubs("Ohio")),
    "WI": StateConfig(alpha="WI", name="Wisconsin", commodity_label="corn",
                       channels=_corn_channels(), clean_text_start_year=2015, base_prefixes=_hubs("Wisconsin")),
    "MO": StateConfig(alpha="MO", name="Missouri", commodity_label="corn",
                       channels=_corn_channels(), clean_text_start_year=2006, base_prefixes=_hubs("Missouri")),
    "MI": StateConfig(alpha="MI", name="Michigan", commodity_label="corn",
                       channels=_corn_channels(), clean_text_start_year=2004, base_prefixes=_hubs("Michigan")),
    "PA": StateConfig(alpha="PA", name="Pennsylvania", commodity_label="corn",
                       channels=_corn_channels(), clean_text_start_year=2008, base_prefixes=_hubs("Pennsylvania")),
    "KY": StateConfig(alpha="KY", name="Kentucky", commodity_label="corn",
                       channels=_corn_channels(), clean_text_start_year=2005, base_prefixes=_hubs("Kentucky"),
                       year_folder_fmt="cw{yy:02d}"),
}
