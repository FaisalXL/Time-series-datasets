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


def cdx_list_all_pdfs(prefix: str) -> Optional[list[tuple[str, str]]]:
    """Whole-folder CDX query: every real (status-200) PDF captured anywhere under `prefix/`,
    across ALL years and subfolder layouts at once. This is layout-agnostic -- it catches reports
    whether they sit in `{year}/`, `prevCW/{year}/`, `{year}_PDF/`, `cw{yy}/`, or flat in the
    folder root -- unlike the per-year `cdx_list_year_pdfs`, which only finds `{year}/`-nested
    files and silently returned 0 for states like Texas (flat-root `txcw4111.pdf`) and Georgia
    (`2007_PDF/`) even though their archives are large and real. The caller parses each report's
    OWN embedded week-ending date, so the on-disk layout doesn't matter for correctness.

    Returns None (not []) if the query itself failed, so the caller can distinguish a genuinely
    empty archive from a transient Wayback failure; retried inside `_http_get`.
    """
    q = (f"{CDX_API}?url={prefix}/&matchType=prefix&output=json"
         f"&filter=statuscode:200&collapse=urlkey&limit=20000")
    raw = _http_get(q, timeout=60)
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    rows = data[1:] if data else []
    out = []
    for row in rows:
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


# An optional day-of-week between the keyword and the date. Louisiana writes every week as
# "Week Ending Sunday, May 15, 2011", which the original pattern could not match at all -- it cost
# all 283 of Louisiana's reports, and they were indistinguishable downstream from "no date here".
_DOW = r"(?:(?:Sun|Mon|Tues|Tue|Wednes|Wednes|Thurs|Thur|Fri|Satur|Sat)day\.?,?\s+)?"
_WEEK_ENDING_RE = re.compile(
    rf"week ending:?\s+{_DOW}([A-Za-z]+\.?\s+\d{{1,2}},?\s+\d{{4}})", re.IGNORECASE
)
# Numeric form. Separator may be / . or - : Nebraska's historical era writes "Week Ending 12-3-51".
_WEEK_ENDING_NUMERIC_RE = re.compile(
    rf"week ending:?\s+{_DOW}(\d{{1,2}})[/.-](\d{{1,2}})[/.-](\d{{2,4}})", re.IGNORECASE
)
# Year-less form: "WEEK ENDING JULY 16", "week ending May 21". Real and common (Ohio's narrative,
# Indiana's older era, and Wisconsin 2001-2014 -- which the README recorded as unrecoverable
# depth). The year is recovered from elsewhere in the document by `_resolve_yearless`.
_WEEK_ENDING_NO_YEAR_RE = re.compile(
    rf"week ending:?\s+{_DOW}([A-Za-z]+)\.?\s+(\d{{1,2}})(?![\d/.-])", re.IGNORECASE
)
_ANY_YEAR_RE = re.compile(r"\b(19[3-9]\d|20[0-4]\d)\b")
# Indiana's oldest era (~1997-2000) states the date bare, top-of-document, with no "week
# ending"/"released:" keyword anywhere nearby (e.g. "August 7, 2000\nReleased: Monday, 3PM" --
# "Released:" is followed by a day-of-week + time, not a date). Restricted to the first few
# lines only, to avoid matching an unrelated date mentioned later in the body prose.
_BARE_DATE_RE = re.compile(r"([A-Za-z]+\.?\s+\d{1,2},?\s+\d{4})")
_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]
# Full names plus the abbreviations states actually print. Idaho writes "Week Ending: Aug 13,
# 2006"; with full names only, the pattern matched and then the month lookup silently failed, so
# the report landed in `no_date` looking like it had no date at all.
_MONTHS = {m.lower(): i for i, m in enumerate(_MONTH_NAMES) if m}
for _i, _m in enumerate(_MONTH_NAMES):
    if not _m:
        continue
    _MONTHS.setdefault(_m[:3].lower(), _i)
_MONTHS["sept"] = 9


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
            # Pivot on "not in the future" rather than a fixed cut. A fixed <70 -> 2000s rule read
            # Nebraska's "Week Ending 12-3-51" as 2051.
            year = year + 2000 if year + 2000 <= dt.date.today().year else year + 1900
        try:
            return dt.date(year, int(mm), int(dd))
        except ValueError:
            pass

    # An explicit "week ending <Month> <Day>" with the year omitted, resolved from a year stated
    # elsewhere in the document. This runs BEFORE the bare-date fallback: the bare-date rule scans
    # the head of the document and will happily return the *release* date ("Issued July 18, 2022")
    # when the real week-ending line sits just below it.
    m4 = _WEEK_ENDING_NO_YEAR_RE.search(text)
    if m4:
        mon = _MONTHS.get(m4.group(1).rstrip(".").lower())
        if mon:
            d = _resolve_yearless(text, mon, int(m4.group(2)))
            if d:
                return d

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


def count_week_ending_dates(text: str, limit: int = 12) -> int:
    """How many *distinct* week-ending dates the document states.

    Used to reject documents that are not a single weekly report. Two real kinds turn up in these
    archives and both parse a perfectly good date (the first one they mention), so nothing upstream
    catches them:

      * whole-season compilations -- Wisconsin `cwan2001.pdf` / `cw2002.pdf`, 71-80k chars
        containing every week of the year;
      * monthly summaries -- Idaho `Monthly_Feb_2016.pdf`, ~50k chars.

    Paired naively, each becomes a record whose text covers a whole season while its series window
    ends at one arbitrary week — a real alignment defect, not just an oversized record. Counting
    distinct dates is layout- and state-independent, unlike matching filenames. A genuine weekly
    report states one date (occasionally two, when it prints the prior week alongside).
    """
    found: set[dt.date] = set()
    for m in _WEEK_ENDING_RE.finditer(text):
        raw = m.group(1).replace(",", "").strip().split()
        if len(raw) == 3:
            mon = _MONTHS.get(raw[0].rstrip(".").lower())
            if mon:
                try:
                    found.add(dt.date(int(raw[2]), mon, int(raw[1])))
                except ValueError:
                    pass
        if len(found) >= limit:
            break
    for m in _WEEK_ENDING_NUMERIC_RE.finditer(text):
        mm, dd, yy = m.groups()
        y = int(yy)
        if y < 100:
            y = y + 2000 if y + 2000 <= dt.date.today().year else y + 1900
        try:
            found.add(dt.date(y, int(mm), int(dd)))
        except ValueError:
            pass
        if len(found) >= limit:
            break
    return len(found)


def _resolve_yearless(text: str, mon: int, day: int) -> Optional[dt.date]:
    """Recover the year for a "week ending <Month> <Day>" line that omits it.

    Candidate years are the 4-digit years the document itself states (release line, volume line,
    table headers). These reports' weeks end on a **Sunday**, which disambiguates almost every case
    on its own: a given month/day falls on a Sunday only about one year in seven, so among a
    handful of candidates usually exactly one qualifies. Without a unique Sunday-consistent
    candidate we return None rather than guess -- a wrong year silently mis-joins the record to
    another season's series, which is far worse than dropping the report.
    """
    years = []
    for m in _ANY_YEAR_RE.finditer(text[:3000]):
        y = int(m.group(1))
        if y not in years:
            years.append(y)
    if not years:
        return None
    sundays = []
    for y in years:
        try:
            d = dt.date(y, mon, day)
        except ValueError:
            continue
        if d.weekday() == 6:          # Sunday
            sundays.append(d)
    if len(sundays) == 1:
        return sundays[0]
    if not sundays and len(years) == 1:
        try:
            return dt.date(years[0], mon, day)
        except ValueError:
            return None
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


def find_gutter(words: list, page_width: float) -> Optional[float]:
    """Locate a real two-column gutter by projection profile, or return None.

    Replaces a pair of hand-tuned heuristics that traded one error for the other. The original
    rule was "the vertical line crossed by the fewest words"; that fired on ordinary justified
    single-column pages, so a `_has_consistent_gutter_width` test was added requiring >=35% of
    straddling-row gaps to sit within +-3pt of their median. That test then produced the opposite
    error -- it rejects genuine two-column pages whose gutter width varies, e.g. Iowa
    2012-04-01 page 0 (crossing 0.48%, word mass balanced 397/433, unmistakably two columns)
    scored `consistent=False` and fell back to row-order extraction, which interleaves the
    columns mid-sentence: "...eager to plant their crop for the upcoming crop year. Warm
    Provided by Harry Hillaker, State Climatologist".

    What actually separates the two cases is *physical*, and the earlier scouting had already
    measured it: a printed gutter is a continuous empty vertical band 15-18pt wide, while a
    coincidental alignment of word gaps in justified text is only a point or two wide. So find
    the widest contiguous band that essentially no word crosses, and require it to be a
    plausible physical margin with real text on both sides. No threshold here is tuned to a
    single page: the band-width floor is a typographic fact, and the balance floor just says
    "both columns contain text".
    """
    if not words:
        return None
    # Crossing is counted per *text row*, not per word. A real gutter is still crossed by the
    # page's few full-width elements -- a banner heading, a district table that spans both
    # columns -- so a word-level "empty band" test finds nothing (measured on Iowa 2012-04-01:
    # the true gutter region never drops below 4 crossing words out of 830). Per row, those same
    # full-width elements are a small minority, and the separation is unambiguous: the real
    # two-column pages show a 10-12pt band under 15% row-crossing, single-column pages show none.
    rows: dict[int, list] = {}
    for w in words:
        rows.setdefault(round(w["top"] / 3), []).append(w)
    row_list = list(rows.values())
    if len(row_list) < 12:
        return None
    lo, hi = int(page_width * 0.25), int(page_width * 0.75)
    empty: list[bool] = []
    for x in range(lo, hi):
        crossing = sum(1 for rw in row_list if any(w["x0"] < x < w["x1"] for w in rw))
        empty.append(crossing / len(row_list) <= 0.15)
    # widest contiguous empty run
    best_len = best_start = 0
    cur_start = None
    for i, e in enumerate(empty + [False]):
        if e and cur_start is None:
            cur_start = i
        elif not e and cur_start is not None:
            if i - cur_start > best_len:
                best_len, best_start = i - cur_start, cur_start
            cur_start = None
    if best_len < 10:                              # narrower than any real printed gutter
        return None
    gx = lo + best_start + best_len / 2.0
    left = sum(1 for w in words if (w["x0"] + w["x1"]) / 2 < gx)
    right = len(words) - left
    if min(left, right) < 0.25 * len(words):       # a margin, not a column boundary
        return None
    return gx


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
    words_for_detection = [wd for wd in page.extract_words() if wd["top"] > 100]
    if len(words_for_detection) < 20:
        return page.extract_text() or ""

    gx = find_gutter(words_for_detection, page.width)
    if gx is None:
        return page.extract_text() or ""  # genuinely single column

    # Exclude the masthead band (letterhead/address/title, top <= 110) from the column
    # reconstruction entirely -- it spans both columns' width and, if left in, gets shuffled
    # into whichever column its centered words happen to land in, corrupting both.
    left = [w for w in words_for_detection if (w["x0"] + w["x1"]) / 2 < gx]
    right = [w for w in words_for_detection if (w["x0"] + w["x1"]) / 2 >= gx]
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
    r"Cooperating with|In Cooperation with|Field Office|^To access NASS|Media Contact|"
    r"^\d+\s+\w[\w.'\s]*\s+(?:Street|St\.?|Ste\.?|Suite|Blvd\.?|Mall|Walnut)|"
    r"^Mall\s+\w|,\s*[A-Z]{2}\s+\d{5}(-\d{4})?\b)",
    re.IGNORECASE,
)


def _looks_like_prose(stripped: str) -> bool:
    """True when a line reads as a real sentence fragment rather than table furniture.

    Cheap and deliberately conservative: several ordinary alphabetic words, and predominantly
    lower-case letters. Table headers and station rows are short, upper-case, or number-dense.
    """
    words = [w for w in re.findall(r"[A-Za-z']{2,}", stripped)]
    if len(words) < 6:
        return False
    alpha = [c for c in stripped if c.isalpha()]
    if not alpha:
        return False
    return sum(1 for c in alpha if c.islower()) / len(alpha) > 0.75


def _is_tabular_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    digits = len(_DIGIT_RUN_RE.findall(stripped))
    letters = sum(c.isalpha() for c in stripped)
    total = max(len(stripped.replace(" ", "")), 1)
    if digits / total > 0.30 and letters < 20:
        return True
    # ⚠️ The marker test is a substring match, so it fires on ordinary prose that happens to use
    # the word: "TEMPERATURE" matches inside "Temperatures for the week as a whole averaged...",
    # "PRECIPITATION" inside "The statewide average precipitation was 0.17 inches...". These are
    # weather narratives -- half of every record's text is exactly such sentences -- and the
    # unguarded test was discarding them. Measured on a 60-report Iowa sample: 560 real prose
    # lines dropped on marker hits alone. Requiring the line NOT to read as prose keeps the
    # genuine all-caps header rows ("STATION PRECIPITATION TEMPERATURE") that this is for.
    if any(marker in stripped.upper() for marker in _TABLE_HEADER_MARKERS):
        if not _looks_like_prose(stripped):
            return True
    # A weather-station row: "Station Name <temp>" or "Station Name <hi> <lo>" -- Titlecase
    # words followed by only 1-2 short numbers, no other prose content.
    return bool(_STATION_ROW_RE.match(stripped))


def _is_masthead_line(line: str) -> bool:
    return bool(_MASTHEAD_RE.search(line.strip()))


# A dot-leader table row: "Lowden .............. 77 36 52 11" / "Oats planted ...... 56 57 55 64".
_DOT_LEADER_RE = re.compile(r"^\s*(.{2,40}?)\s*\.{3,}\s*([\d\s.,+-]+)$")

# Vocabulary of agricultural terms, used to tell the two kinds of dot-leader row apart. Built from
# the channel vocabulary itself (see `ag_vocabulary`) so it tracks whatever commodities the source
# publishes instead of being a hand-maintained list.
_AG_EXTRA = {
    "range", "pasture", "pastureland", "topsoil", "subsoil", "soil", "moisture", "days",
    "suitable", "fieldwork", "field", "work", "crop", "crops", "condition", "progress",
    "planted", "emerged", "harvested", "seedbed", "tillage", "acreage",
}
_AG_VOCAB: set[str] = set(_AG_EXTRA)


def ag_vocabulary(short_descs) -> set[str]:
    """Words that mark a table row as *crop* data rather than weather-station data.

    Both arrive as dot-leader rows, and they must be treated differently: the per-station weather
    table is noise (and is AWIS-copyrighted third-party content in many states' reports), while the
    crop-progress table row states the very percentages that are the series -- for some states the
    numbers appear only there and not in a prose sentence. Deriving the vocabulary from the
    SHORT_DESC strings being paired against keeps the two apart without a hand-kept crop list and
    without discarding recitation.
    """
    # Commodity heads only, >=4 chars. Including the "MEASURED IN PCT <class>" words pulled in
    # colour and rating adjectives (red, pink, green, good, fair, poor), and a colour word is
    # enough to make a place name look agricultural -- the weather station "Red Oak ..... 86 41
    # 63 19" survived on "red". Heads alone still cover the rows that matter, because a crop
    # table row names its crop ("Oats planted", "Range & Pasture").
    stop = {"excl", "incl", "totals", "other", "open", "measured", "harvested", "prev"}
    vocab = set(_AG_EXTRA)
    for sd in short_descs:
        head = sd.split(" - ", 1)[0]
        for tok in re.findall(r"[A-Za-z]{4,}", head):
            t = tok.lower()
            if t not in stop:
                vocab.add(t)
    return vocab


def set_ag_vocabulary(vocab: set[str]) -> None:
    global _AG_VOCAB
    _AG_VOCAB = set(_AG_EXTRA) | {v.lower() for v in vocab}


def _is_station_row(line: str) -> bool:
    """A dot-leader row whose label names no agricultural term -> a weather-station data row."""
    m = _DOT_LEADER_RE.match(line)
    if not m:
        return False
    label, nums = m.group(1), m.group(2)
    if len(re.findall(r"\d+", nums)) < 2:
        return False
    words = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", label)}
    if not words:
        return True
    return not (words & _AG_VOCAB)


_TABLE_HEADER_WORDS_RE = re.compile(
    r"^\s*(Item|Districts?|State\b|Percent(\s+Percent)+|Days(\s+Days)+|HI\s*LO?\s*$|"
    r"Last\s+Last|Week\s+Year(\s+mal)?|Very\s*$|Poor\b.*Fair.*Good.*Excellent|"
    r"NW\s+NC\s+NE|WC\s+C\s+EC|SW\s+SC\s+SE|NA\s*=\s*[Nn]ot)",
)
# Column-unit header rows, which survive once the numbers beneath them are stripped:
# "(Percent) (Percent) (Percent) ..." or "(Days) (Days) ...", possibly clipped at a column edge.
_UNIT_HEADER_RE = re.compile(
    r"^[\s\W]*(\(?\s*(?:Percent|Pct|Days|Inches|Degrees)\s*\)?[\s,]*){2,}$", re.IGNORECASE,
)
# District abbreviation header rows anywhere in the line ("EC SW SC SE Week Year"), not just at
# the start -- the anchored alternatives above miss them whenever the row begins mid-table.
_DISTRICT_ABBR_RE = re.compile(
    r"\b(NW|NC|NE|WC|EC|SW|SC|SE|C)\b(\s+\b(NW|NC|NE|WC|EC|SW|SC|SE|C)\b){2,}",
)
# Legend / column-definition lines printed beneath the weather table. Grammatically prose, so the
# prose guard keeps them, but they define the stripped table's columns rather than narrating
# anything: "Precipitation (rain, melted snow or ice) in inches. Precipitation Days =",
# "Days with precipitation of 0.01 inch or more. Air Temperatures in Degrees".
_LEGEND_RE = re.compile(
    r"(Precipitation\s*\(rain|Precipitation\s+Days\s*=|Air\s+Temperatures?\s+in\s+Degrees|"
    r"DFN\s*=|GDD\s*=|Departure\s+from\s+[Nn]ormal|"
    r"^\s*Days\s+with\s+precipitation\s+of|Growing\s+Degree\s+Days\s+base)",
)
_REVERSED_MARKERS = ("POSTAGE", "PERIODICALS", "POSTMASTER", "NEWSPAPER", "SUBSCRIPTION",
                     "MAILING OFFICE", "ADDITIONAL", "WALNUT", "ISSN", "PUBLISHED WEEKLY",
                     "SIOUX FALLS", "DES MOINES")
_ATTRIBUTION_RE = re.compile(
    r"^\s*(Fahrenheit\.?\s*Copyright|Copyright\s+\d{4}|[A-Z]+,?\s*Inc\.?\s*All Rights Reserved|"
    r"All Rights Reserved\.?\s*$|Degrees Fahrenheit\.?\s*Copyright)",
)
# Bare district row-labels left behind once a district table's numbers are stripped
# ("North West District", "East Central District", ...) -- a label column, not narrative.
_DISTRICT_LABEL_RE = re.compile(
    r"^\s*(North|South|East|West|Central|North\s?West|North\s?East|South\s?West|South\s?East|"
    r"North\s?Central|South\s?Central|East\s?Central|West\s?Central)(\s+(Central|District))?\s*$",
    re.IGNORECASE,
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
        and not _is_station_row(ln)
        and not _TABLE_HEADER_WORDS_RE.search(ln)
        and not _looks_reversed_boilerplate(ln)
        and not _ATTRIBUTION_RE.search(ln)
        and not _DISTRICT_LABEL_RE.match(ln)
        and not _UNIT_HEADER_RE.match(ln)
        and not _DISTRICT_ABBR_RE.search(ln)
        and not _LEGEND_RE.search(ln)
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
    # Where this state's narratives come from, when not its own archive. Only New England uses
    # this: six states share one regional publication, so the PDFs are fetched once under the
    # "NEWENG" pool instead of six times.
    text_pool: str | None = None
    # False for the New England regional pool itself, which is a text source with no series.
    emits_records: bool = True

    @property
    def pool(self) -> str:
        return self.text_pool or self.alpha

    def discover_year(self, year: int) -> list[tuple[str, str]]:
        """Per-year discovery under `{prefix}/{year_folder}/`. Kept for back-compat / targeted
        single-year smoke tests, but NOTE it only finds reports nested under a year-shaped
        subfolder; `discover_all` (below) is the layout-agnostic default the build now uses,
        because an all-state ground round found many states (Texas, Georgia, ...) whose recent
        reports sit flat in the folder root or under `prevCW/{year}`, `{year}_PDF`, etc. -- which
        this per-year query silently missed (returned 0)."""
        hits: list[tuple[str, str]] = []
        for prefix in self.base_prefixes:
            hits.extend(cdx_list_year_pdfs(prefix, year, self.year_folder_fmt))
        return hits

    def discover_all(self) -> tuple[list[tuple[str, str]], bool]:
        """Layout-agnostic discovery: every real PDF captured anywhere under the state's hub
        folder(s), across all years/subfolder schemes at once, deduped by URL (keeping the
        earliest 200 timestamp per URL). The caller parses each report's own embedded
        week-ending date and buckets by that, so no per-state folder-layout knowledge is needed.

        Returns (candidates, ok). `ok` is False if EVERY base-folder query failed (transient
        Wayback error) -- distinct from a genuinely empty archive (ok True, empty list) -- so a
        build won't mistake a Wayback outage for "this state has no data"."""
        seen: dict[str, str] = {}
        any_ok = False
        for prefix in self.base_prefixes:
            res = cdx_list_all_pdfs(prefix)
            if res is None:
                continue  # this folder-variant query failed; try the others
            any_ok = True
            for ts, url in res:
                seen.setdefault(url, ts)
        return [(ts, url) for url, ts in seen.items()], any_ok


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


_CONDITION_CLASSES = ["VERY POOR", "POOR", "FAIR", "GOOD", "EXCELLENT"]


def _condition_channels(commodity_short: str, unit_prefix: str) -> list[Channel]:
    """Full 5-way condition rating (very poor -> excellent). NASS surveys crop condition every
    week the crop is in the ground -- far denser than the cascading growth-stage PROGRESS
    channels (each stage is only surveyed during its own ~few-week window) -- and the weekly
    narrative recites all five classes verbatim ("rated 8 percent poor, 45 percent fair, 44
    percent good, and 3 percent excellent"). Originally only good+excellent were emitted; the
    other three are equally real, equally recited, and add both density and alignment coverage.
    good/excellent keep their original unit names for continuity with earlier output."""
    return [
        Channel(f"{commodity_short} - CONDITION, MEASURED IN PCT {cls}",
                f"{unit_prefix}_condition_pct_{cls.lower().replace(' ', '_')}")
        for cls in _CONDITION_CLASSES
    ]


def _soil_moisture_channels() -> list[Channel]:
    """Topsoil + subsoil moisture, 4 classes each (very short/short/adequate/surplus). NOT
    crop-specific -- reported once per state per week regardless of commodity -- and surveyed
    EVERY week of the season (confirmed 100% dense across a full Iowa 2003 season, vs. 21-24%
    for the growth-stage channels). The weekly narrative recites them verbatim ("Topsoil
    moisture was rated 1 percent very short, 8 percent short, 78 percent adequate, and 13
    percent surplus"). These are the channels that make each record a genuinely dense weekly
    series rather than a mostly-null per-stage cascade -- so they anchor every state/commodity."""
    return [
        Channel(f"SOIL, {layer} - MOISTURE, MEASURED IN PCT {cls}",
                f"{layer.lower()}_moisture_pct_{cls.lower().replace(' ', '_')}")
        for layer in ("TOPSOIL", "SUBSOIL")
        for cls in ("VERY SHORT", "SHORT", "ADEQUATE", "SURPLUS")
    ]


def _corn_channels() -> list[Channel]:
    stages = ["PLANTED", "EMERGED", "SILKING", "DOUGH", "DENTED", "MATURE"]
    chans = [Channel(f"CORN - PROGRESS, MEASURED IN PCT {s}", f"corn_pct_{s.lower()}") for s in stages]
    chans.append(Channel("CORN, GRAIN - PROGRESS, MEASURED IN PCT HARVESTED", "corn_pct_harvested"))
    chans += _condition_channels("CORN", "corn")
    chans.append(Channel("FIELDWORK - DAYS SUITABLE, MEASURED IN DAYS / WEEK", "days_suitable_per_week"))
    chans += _soil_moisture_channels()
    return chans


def _wheat_channels() -> list[Channel]:
    stages = ["PLANTED", "EMERGED", "JOINTING", "HEADED", "COLORING", "MATURE", "HARVESTED"]
    chans = [Channel(f"WHEAT, WINTER - PROGRESS, MEASURED IN PCT {s}", f"winter_wheat_pct_{s.lower()}")
             for s in stages]
    chans += _condition_channels("WHEAT, WINTER", "winter_wheat")
    chans.append(Channel("FIELDWORK - DAYS SUITABLE, MEASURED IN DAYS / WEEK", "days_suitable_per_week"))
    chans += _soil_moisture_channels()
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

# --- Second-wave bulk rollout (2026-07-29): 27 more states wired from an all-state archive scout.
# Each state's Wayback archive was confirmed to exist (real status-200 PDF count in parens below)
# and `clean_text_start_year` is a CONFIRMED-CLEAN floor -- the earliest year a fetched sample
# actually parsed to clean dated crop-progress prose. These floors are deliberately CONSERVATIVE:
# the archived-PDF counts show most archives run substantially deeper, but the earlier years were
# not all confirmed clean (Wayback was flaky during scouting, and per-state garbled/scanned-image
# boundaries were NOT checked the way the first 12 states were). A full server build can extend
# each floor backward after confirming the earlier years parse clean (the per-state garbled-boundary
# check). Commodity is the state's headline crop that actually has PROGRESS data (corn everywhere it
# has >=300 rows; California is winter wheat -- corn is 0 there). The dense soil-moisture backbone
# is crop-agnostic, so every record is a healthy weekly series regardless of the stage-channel crop.
# base_prefixes uses the shared _hubs() 4-variant list (the build finds whichever folder each state
# actually uses); none of these needed KY's cw{yy} nesting.
# (alpha, NASS folder name, commodity_label, channels-builder, clean_text_start_year)
_SECOND_WAVE = [
    ("AL", "Alabama",        "corn",         _corn_channels,  2007),   # 827 PDFs
    ("AR", "Arkansas",       "corn",         _corn_channels,  2012),   # 794
    ("CA", "California",     "winter wheat", _wheat_channels, 2005),   # 1422; corn=0, winter wheat=1872
    ("CO", "Colorado",       "corn",         _corn_channels,  2006),   # 916
    ("DE", "Delaware",       "corn",         _corn_channels,  2005),   # 749
    ("GA", "Georgia",        "corn",         _corn_channels,  2015),   # 603; NO clean sample parsed -> verify
    ("ID", "Idaho",          "corn",         _corn_channels,  2004),   # 650
    ("LA", "Louisiana",      "corn",         _corn_channels,  2019),   # 733
    ("MD", "Maryland",       "corn",         _corn_channels,  2005),   # 742
    ("MS", "Mississippi",    "corn",         _corn_channels,  2001),   # 1179
    ("MT", "Montana",        "corn",         _corn_channels,  2005),   # 904
    ("NC", "North_Carolina", "corn",         _corn_channels,  2016),   # 267
    ("ND", "North_Dakota",   "corn",         _corn_channels,  2005),   # 884
    ("NJ", "New_Jersey",     "corn",         _corn_channels,  2015),   # 884; NO clean sample parsed -> verify
    ("NM", "New_Mexico",     "corn",         _corn_channels,  2005),   # 1103
    ("NY", "New_York",       "corn",         _corn_channels,  2015),   # 980; NO clean sample parsed -> verify
    ("OK", "Oklahoma",       "corn",         _corn_channels,  2005),   # 829
    ("OR", "Oregon",         "corn",         _corn_channels,  2005),   # 554
    ("SC", "South_Carolina", "corn",         _corn_channels,  2006),   # 906
    ("SD", "South_Dakota",   "corn",         _corn_channels,  2006),   # 764
    ("TN", "Tennessee",      "corn",         _corn_channels,  2014),   # 734
    ("TX", "Texas",          "corn",         _corn_channels,  2003),   # 1902
    ("UT", "Utah",           "corn",         _corn_channels,  2018),   # 877
    ("VA", "Virginia",       "corn",         _corn_channels,  2021),   # 820
    ("WA", "Washington",     "corn",         _corn_channels,  2001),   # 759
    ("WV", "West_Virginia",  "corn",         _corn_channels,  2002),   # 267
    ("WY", "Wyoming",        "corn",         _corn_channels,  2006),   # 910
]
for _alpha, _folder, _label, _chans, _start in _SECOND_WAVE:
    STATE_CONFIGS[_alpha] = StateConfig(
        alpha=_alpha, name=_folder.replace("_", " "), commodity_label=_label,
        channels=_chans(), clean_text_start_year=_start, base_prefixes=_hubs(_folder),
    )

# --- Third wave (2026-07-30): the nine states previously documented as deferred.
# Both reasons for deferring them turned out to be removable, and one of the two was an artifact
# of a throttled scout rather than a fact about the source:
#
#   AZ, FL, NV were deferred as needing "a new crop-stage channel set, not just a config entry".
#     That was true of the hand-written _corn_channels/_wheat_channels design; channel selection is
#     now derived from the series index (scripts/channels.py), so a state needs no per-commodity
#     code at all. They report large weekly series -- AZ upland cotton 1,660 weeks, FL peanuts 966,
#     NV pastureland 944 -- and their archives are large (AZ 1,225 PDFs, FL 1,354, NV 210).
#     ⚠️ An earlier concurrent probe returned 0 PDFs for all three, which is what "~0 corn" got
#     conflated with; re-probed under the shared rate limiter they are anything but empty.
#
#   CT, MA, ME, NH, RI, VT were deferred because "per-state Wayback folders have zero archive".
#     The per-state folders are indeed empty -- but the shared regional office publishes under
#     `New_England_includes/`, which holds 979 archived PDFs, and Quick Stats still carries
#     per-STATE weekly series for all six (~870 weeks of pastureland/apples/potatoes each). The
#     regional report is fetched ONCE into the `NEWENG` pool and the six states read their text
#     from it (see `text_pool`), rather than fetching the same PDFs six times.
#
#   ⚠️ New England was investigated and then EXCLUDED again, on alignment rather than availability
#     -- see the note under `_NEW_ENGLAND` below. The archive is real and is harvested; what is
#     missing is a series the regional prose is actually about.
_THIRD_WAVE = [
    ("AZ", "Arizona", None),
    ("FL", "Florida", None),
    ("NV", "Nevada", None),
]
for _alpha, _folder, _pool in _THIRD_WAVE:
    STATE_CONFIGS[_alpha] = StateConfig(
        alpha=_alpha, name=_folder.replace("_", " "), commodity_label="derived",
        channels=[], clean_text_start_year=1979, base_prefixes=_hubs(_folder),
        text_pool=_pool,
    )

# The six New England states: archive found, records NOT emitted.
#
# The blocker documented previously ("per-state Wayback folders are empty") is true but was not the
# real one. The shared regional office publishes 979 archived reports under `New_England_includes/`,
# and Quick Stats carries per-STATE weekly series for all six (~870 weeks each). So both halves
# exist -- but they are not about each other:
#
#   * The regional report's prose states REGIONAL aggregates: "there were 5.7 days available for
#     field work across New England. Pasture condition was rated 12% poor, 20% fair, 58% good, and
#     10% excellent." Those numbers are not any one state's.
#   * Quick Stats has no New England aggregate to pair that prose with. Checked exhaustively across
#     the whole file: WEEKLY rows exist at AGG_LEVEL `STATE` (3,352,511), `NATIONAL` (120,649) and
#     `REGION : SUB-STATE` (6,713, entirely Colorado potato districts). There is no New England
#     region row.
#   * Per-state content in the report is table-only and thin -- rows like "Maine 5 5 5 Fair/Good",
#     a few numbers and a qualitative rating, with no 5-way percentage breakdown.
#
# Pairing regional prose with a single state's 52-week window would be grounding in name only --
# the schema's "metadata dressed up as language grounding" case -- so these six are excluded. The
# reports stay harvested so a future pass can revisit if NASS publishes a regional series.
_NEW_ENGLAND = [("CT", "Connecticut"), ("MA", "Massachusetts"), ("ME", "Maine"),
                ("NH", "New_Hampshire"), ("RI", "Rhode_Island"), ("VT", "Vermont")]
for _alpha, _folder in _NEW_ENGLAND:
    STATE_CONFIGS[_alpha] = StateConfig(
        alpha=_alpha, name=_folder.replace("_", " "), commodity_label="derived",
        channels=[], clean_text_start_year=1979, base_prefixes=_hubs(_folder),
        text_pool="NEWENG", emits_records=False,
    )

# The shared New England regional office. Not a state: it owns no series of its own, it exists so
# the 979 regional reports are discovered and fetched exactly once. `series_state` is None, so the
# build never tries to emit records for "NEWENG" itself.
STATE_CONFIGS["NEWENG"] = StateConfig(
    alpha="NEWENG", name="New England (regional office)", commodity_label="derived",
    channels=[], clean_text_start_year=1979,
    base_prefixes=_hubs("New_England_includes"), emits_records=False,
)

