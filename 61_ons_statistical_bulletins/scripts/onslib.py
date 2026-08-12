"""Shared ONS parsing: dataset CSVs, datelines, prose sections, claims, and the naming rule.

This module carries the bug history of the package. Every non-obvious rule below exists
because output was checked against source and found wrong:

  * A naive `[^.!?]+` sentence split treats the "." in "2.8%" as a sentence end, shredding
    every figure. The first evidence pass returned 0 matches with 0 rejections -- a silent
    total failure, not a crash.
  * A symmetric +/-140-char keyword radius reached into neighbouring sentences and credited a
    "core CPIH" figure to headline CPI. Attribution is clause-scoped for exactly this reason.
  * "Download this chart Figure 3: ..." glued page chrome into prose. Killed by a prefix match
    PLUS a structural rule: a real prose paragraph ends in terminal punctuation.
  * The URL slug is NOT the reference month. ONS edition slugs come in 14 shapes
    (`june2026`, `6august2026`, `weekending12may2023`, `2019to2020`), and slug-derived dates
    were wrong on FHFA #59. The reference period is read from the document's own h1.
  * Value equality alone is not evidence. Matching figures against all ~4,000 series in a
    dataset produced 259k matches whose month-shifted control produced 189k -- no
    discrimination whatsoever. `names_series` is the rule that fixed it; see its docstring.
"""
from __future__ import annotations

import bisect
import collections
import csv
import html as htmllib
import io
import pickle
import re

from onsfetch import CACHE, fetch

MONTHS = ["", "january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
ABBR = {m[:3].upper(): i for i, m in enumerate(MONTHS) if m}
PARSED = CACHE / "parsed"


# ============================ dataset CSVs ==================================================
# One CSV per dataset holds EVERY series in the family with full history (mm23.csv: 4,053
# series x 450 months, 23MB). That is one fetch instead of one per CDID, and it is what makes
# value-verified channel discovery possible at all -- there is no keyword guessing, because
# the whole candidate universe is present.

def parse_dataset_csv(raw: bytes) -> dict:
    """-> {cdid: {title, unit, m:{'YYYY-MM':v}, q:{'YYYY-Qn':v}, y:{'YYYY':v}}}"""
    rows = list(csv.reader(io.StringIO(raw.decode("utf8", "replace"))))
    if not rows or not rows[0] or rows[0][0].strip().lower() != "title":
        return {}
    hdr = {r[0].strip().lower(): r for r in rows[:8] if r}
    titles = rows[0][1:]
    cdids = [c.strip().lower() for c in (hdr.get("cdid") or [""])[1:]]
    units = (hdr.get("unit") or [""])[1:]
    out = {}
    for i, cd in enumerate(cdids):
        if cd and cd not in out:
            out[cd] = {"title": (titles[i] if i < len(titles) else "").strip(),
                       "unit": (units[i] if i < len(units) else "").strip(),
                       "m": {}, "q": {}, "y": {}}
    cols = [(cd, i + 1) for i, cd in enumerate(cdids) if cd]
    for r in rows[7:]:
        if not r or not r[0].strip():
            continue
        lbl = r[0].strip().upper()
        if (m := re.fullmatch(r"(\d{4})\s+([A-Z]{3})", lbl)) and m.group(2) in ABBR:
            bucket, key = "m", f"{m.group(1)}-{ABBR[m.group(2)]:02d}"
        elif m := re.fullmatch(r"(\d{4})\s+Q(\d)", lbl):
            bucket, key = "q", f"{m.group(1)}-Q{m.group(2)}"
        elif re.fullmatch(r"\d{4}", lbl):
            bucket, key = "y", lbl
        else:
            continue
        n = len(r)
        for cd, i in cols:
            if i >= n:
                continue
            v = r[i].strip()
            if not v:
                continue
            try:
                out[cd][bucket][key] = float(v)
            except ValueError:
                pass
    return out


def load_dataset(uri_path: str, ds_id: str, version: str | None = None) -> dict:
    """Load+cache a parsed dataset. `version` picks a historical vintage (e.g. 'v135')."""
    sub = f"current/previous/{version}" if version else "current"
    url = f"https://www.ons.gov.uk/file?uri=/{uri_path}/{sub}/{ds_id}.csv"
    pk = PARSED / f"{ds_id}__{version or 'current'}.pkl"
    if pk.exists():
        try:
            return pickle.loads(pk.read_bytes())
        except Exception:
            pass
    code, raw = fetch(url)
    if code != 200 or not raw:
        return {}
    d = parse_dataset_csv(raw)
    if d:
        pk.parent.mkdir(parents=True, exist_ok=True)
        pk.write_bytes(pickle.dumps(d, protocol=4))
    return d


# ============================ the document's own dateline ====================================

_H1 = re.compile(r"(?is)<h1[^>]*>(.*?)</h1>")
MONTH_ALT = "|".join(m for m in MONTHS if m)


def page_title(page: bytes) -> str:
    d = page.decode("utf8", "replace")
    m = _H1.search(d) or re.search(r"(?is)<title>(.*?)</title>", d)
    if not m:
        return ""
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
    return re.sub(r"\s*-\s*Office for National Statistics\s*$", "", t).strip()


def coverage_period(title: str) -> tuple[str | None, str]:
    """The period the DOCUMENT ITSELF claims to cover -> ('YYYY-MM'|'YYYY-Qn'|None, shape).

    Read from the h1 ("Consumer price inflation, UK: June 2026"), never from the URL slug. A
    shared slug pattern is a smell, not evidence: these slugs come in 14 shapes and the URL
    month was the index month + 2 on FHFA #59. For a range ("April to June 2026") the period
    END is returned, which is the month the prose's headline figures refer to.
    """
    tail = title.split(":", 1)[1] if ":" in title else title
    t = tail.strip().lower()
    pats = [
        (rf"\b({MONTH_ALT})\s+(\d{{4}})\s+to\s+({MONTH_ALT})\s+(\d{{4}})", "span_years"),
        (rf"\b({MONTH_ALT})\s+to\s+({MONTH_ALT})\s+(\d{{4}})", "month_to_month"),
        (rf"\bweek ending\s+\d{{1,2}}\s+({MONTH_ALT})\s+(\d{{4}})", "week_ending"),
        (rf"\byear ending\s+({MONTH_ALT})\s+(\d{{4}})", "year_ending"),
        (rf"\bquarter\s+\d[^\d]{{0,24}}({MONTH_ALT})\s+(\d{{4}})", "quarter_named"),
        (rf"\b({MONTH_ALT})\s+(\d{{4}})", "month"),
        (r"\b(?:q|quarter\s*)([1-4])\s+(\d{4})", "quarter"),
        (r"\b(\d{4})\s*(?:to|-|/)\s*(\d{2,4})\b", "year_range"),
        (r"\b(\d{4})\b", "year_only"),
    ]
    for pat, shape in pats:
        m = re.search(pat, t)
        if not m:
            continue
        g = m.groups()
        if shape == "span_years":
            return f"{int(g[3]):04d}-{MONTHS.index(g[2]):02d}", shape
        if shape == "month_to_month":
            return f"{int(g[2]):04d}-{MONTHS.index(g[1]):02d}", shape
        if shape in ("week_ending", "year_ending", "quarter_named", "month"):
            return f"{int(g[-1]):04d}-{MONTHS.index(g[-2]):02d}", shape
        if shape == "quarter":
            return f"{int(g[1]):04d}-Q{g[0]}", shape
        return None, shape
    return None, "none"


# ============================ bulletin prose =================================================

def strip_tags(frag: str) -> str:
    frag = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", frag)
    frag = re.sub(r"(?is)<br\s*/?>", "\n", frag)
    frag = re.sub(r"(?is)</(p|li|h[1-6]|div|tr)>", "\n\n", frag)
    frag = re.sub(r"(?s)<[^>]+>", " ", frag)
    frag = htmllib.unescape(frag)
    frag = re.sub(r"[ \t ]+", " ", frag)
    return re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", frag).strip()


def parse_sections(page: bytes) -> list[dict]:
    """Split a bulletin into its H2 sections.

    Structure (verified live): <div id="{anchor}" class="section__content--markdown">
                                 <section><header><h2><span>N.</span> Title</h2></header> ...
    """
    doc = page.decode("utf-8", "replace")
    out = []
    parts = re.split(r'<div id="([a-z0-9\-]+)"\s+class="section__content--markdown">', doc)
    for i in range(1, len(parts) - 1, 2):
        anchor, body = parts[i], parts[i + 1]
        h2 = re.search(r"(?is)<h2[^>]*>(.*?)</h2>", body)
        title = strip_tags(h2.group(1)) if h2 else anchor
        title = re.sub(r"^\d+\.\s*", "", title).strip()
        body = re.sub(r"(?is)<header>.*?</header>", " ", body, count=1)
        body = re.sub(r"(?is)<table.*?</table>", " ", body)   # numeric dumps, not prose
        out.append({"anchor": anchor, "title": title, "text": strip_tags(body)})
    return out


CHROME_RE = re.compile(
    r"(?i)^\s*(download (this|the) (chart|image|data)|source: |embed code|"
    r"figure \d+[:.]|table \d+[:.]|notes?: |back to table of contents|copy (this )?link|"
    r"view online|hide|show|xlsx|csv|xlsx?\b|\.csv\b)")


def clean_paragraphs(text: str) -> list[str]:
    """Keep real prose paragraphs only.

    Chart furniture is the trap: an ONS section interleaves figure captions and "Download this
    chart Figure 3: ..." lines with the narrative. RBNZ #60 shipped exactly this class of
    leftover chrome glued into a sentence, so both a prefix match AND a structural rule apply:
    a real prose paragraph ENDS IN TERMINAL PUNCTUATION. Captions, axis labels and
    sub-headings do not, so they drop out cleanly (the cost is losing verbatim sub-headings,
    which carry no numbers).
    """
    paras = []
    for p in re.split(r"\n\s*\n", text):
        p = re.sub(r"\s+", " ", p).strip()
        if not p or CHROME_RE.match(p):
            continue
        if len(p) < 25:
            continue
        if not re.search(r"[.!?][\"')’”]?$", p):
            continue
        paras.append(p)
    return paras


def split_clauses(text: str) -> list[tuple[int, str]]:
    """Split into attribution units: sentences, then semicolon-joined sub-claims.

    ONS chains claims about DIFFERENT series with semicolons -- "Core CPIH ... rose by 2.8%
    ...; the CPIH goods annual rate slowed from 2.0% to 1.7%" -- so a sentence-level window
    would let the "core" claim and the "goods" claim satisfy each other. The semicolon split
    is required, not cosmetic. A sentence break requires terminator + whitespace + an opening
    capital, because the "." in "2.8%" is not a sentence end.
    """
    bounds = [0]
    for m in re.finditer(r'(?<=[.!?])\s+(?=[A-Z"‘“(])|\n{2,}', text):
        bounds.append(m.end())
    bounds.append(len(text))
    out = []
    for a, b in zip(bounds, bounds[1:]):
        sent, pos = text[a:b], 0
        for piece in sent.split(";"):
            out.append((a + pos, piece))
            pos += len(piece) + 1
    return [(o, c) for o, c in out if c.strip()]


# ============================ claims =========================================================
# Broadened from percent-only: ONS recites £ billions, counts and index points too, and a
# percent-only reader leaves most of a trade or public-finances bulletin unmatched. But the
# type MATTERS: an untyped number sweep turned 520 real claims into 2,815, of which 2,295 were
# years, list indices and "12 months" -- pure noise that buried the signal.

NUM = r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?"
CLAIM_RE = re.compile(
    rf"(?P<cur>£)?\s*(?P<num>{NUM})\s*"
    rf"(?P<suf>%|per cent|percentage points?|percent|billion|bn\b|million|mn\b|thousand|"
    rf"percentage point|index points?|points?)?", re.I)
SCALE = {"billion": 1e9, "bn": 1e9, "million": 1e6, "mn": 1e6, "thousand": 1e3}
TYPED = {"pct", "pp", "points", "money", "count"}


def claims(clause: str):
    """Yield (value, kind, scale, numtext) for TYPED numeric claims in a clause."""
    for m in CLAIM_RE.finditer(clause):
        num = m.group("num")
        if not num or not re.search(r"\d", num):
            continue
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        suf = (m.group("suf") or "").lower().strip()
        if suf in ("%", "per cent", "percent"):
            kind = "pct"
        elif suf.startswith("percentage point"):
            kind = "pp"
        elif suf in SCALE:
            kind = "money" if m.group("cur") else "count"
        elif m.group("cur"):
            kind = "money"
        elif suf.startswith("point") or suf.startswith("index point"):
            kind = "points"
        else:
            kind = "bare"
        yield v, kind, SCALE.get(suf, 1.0), num


MONTH_IN_CLAUSE = re.compile(
    r"(?i)\b(?:12 months to|months to|in|for|during|to)\s+"
    rf"({MONTH_ALT})(?:\s+(\d{{4}}))?")
PREV_PHRASE = re.compile(r"(?i)\b(?:the previous month|a month earlier|last month)\b")
FROM_TO = re.compile(r"(?i)from\s+-?\d+(?:\.\d+)?\s*%?\s*to\s+-?\d+(?:\.\d+)?\s*%")


def prev_month(ref: str) -> str:
    y, m = int(ref[:4]), int(ref[5:7])
    return f"{y-1:04d}-12" if m == 1 else f"{y:04d}-{m-1:02d}"


def shift_month(key: str, n: int) -> str:
    if not n:
        return key
    y, m = int(key[:4]), int(key[5:7]) + n
    y += (m - 1) // 12
    return f"{y:04d}-{(m - 1) % 12 + 1:02d}"


def clause_target_months(clause: str, ref: str, i: int, n: int) -> list[tuple[str, str]]:
    """Which month(s) a figure inside this clause may refer to.

    Restricting this is what stops a figure being silently credited to the wrong month -- a
    real false match from the first build credited a June CPIH monthly figure to May's CPI
    monthly value.
    """
    prev = prev_month(ref)
    named = []
    for m in MONTH_IN_CLAUSE.finditer(clause):
        mi = MONTHS.index(m.group(1).lower())
        yr = int(m.group(2)) if m.group(2) else int(ref[:4])
        # an unqualified month later than the reference month belongs to the previous year
        key = f"{yr:04d}-{mi:02d}"
        if not m.group(2) and key > ref:
            key = f"{yr-1:04d}-{mi:02d}"
        named.append(key)
    if FROM_TO.search(clause) and n >= 2:
        if i == 0:
            return [("previous_month", prev)]
        if i == 1:
            return [("reference_month", ref)]
    if PREV_PHRASE.search(clause):
        if i == n - 1 and n >= 2:
            return [("previous_month", prev)]
        return [("reference_month", ref)]
    if named:
        return [("named_month", k) for k in dict.fromkeys(named)]
    return [("reference_month", ref), ("previous_month", prev)]


# ============================ the naming rule ================================================
# THE rule that makes alignment mean something. Value equality alone matched 259k claim-series
# pairs whose month-shifted control matched 189k -- it discriminated nothing, because
# percentage data has a very high coincidence floor and a dataset holds thousands of variants
# of the same concept. Three token classes, because they behave differently.

MEASURE = {"cpi", "cpih", "rpi", "rpij", "rpix", "ppi", "hci", "ooh", "gdp", "awe"}
RESTRICTION = {"excluding", "excl", "excludes", "contribution", "contributions", "core",
               "seasonal", "unprocessed", "nonseasonal", "imputed", "equivalent"}
# Restrictions that mean the prose is talking about a DIFFERENT series, not a differently
# presented one. Guarded in both directions; see names_series.
REVERSE_GUARD = {"core", "excluding", "excl", "excludes", "contribution", "contributions"}
GENERIC = set("""the a an of and or in to for by on at with from all items index indices rate
rates annual monthly quarterly month months mth mths percentage change over level levels other
value values volume estimate estimates total seasonally adjusted sa nsa per cent percent
weights wts uk gb great britain england wales scotland northern ireland united kingdom
price prices sp series data including million millions billion billions thousand thousands
cvm cp kp bop sca gr grs level levels flows liabs ratio pct cont curr constant current terms
basis basic""".split())
SYN = {"nonalcoholic": ("non-alcoholic", "nonalcoholic"), "tobac": ("tobacco",),
       "bev": ("beverages", "beverage"), "bevs": ("beverages", "beverage"),
       "occupiers": ("occupiers", "occupiers'"), "hhold": ("household",),
       "misc": ("miscellaneous",), "elec": ("electricity",), "furn": ("furniture",),
       "comms": ("communication", "communications"), "rec": ("recreation",),
       "educ": ("education",), "trans": ("transport",), "hlth": ("health",),
       "alc": ("alcohol", "alcoholic"), "restaurants": ("restaurant", "restaurants"),
       "hotels": ("hotel", "hotels"), "clothing": ("clothing", "clothes"),
       "mfg": ("manufacturing",), "constr": ("construction",)}


def _title_words(s: str) -> str:
    s = re.sub(r"\b\d{4}\s*=\s*\d+\b", " ", s.lower())     # "2015=100"
    s = re.sub(r"\b\d+\s*mth\b", " ", s)                    # "12mth" / "1 mth"
    s = re.sub(r"\b\d+(\.\d+)*\b", " ", s)                  # COICOP codes 01.1.1.1
    return re.sub(r"[^a-z\s'&,:/-]", " ", s)


def parse_title(title: str) -> dict:
    """Split a series title into measure / restriction / concept-segment tokens."""
    t = _title_words(title)
    concept = t.split(":", 1)[1] if ":" in t else t
    measures, restr = set(), set()
    for w in re.findall(r"[a-z][a-z']+", t):
        if w in MEASURE:
            measures.add(w)
        elif w in RESTRICTION:
            restr.add(w)
    segs = []
    for seg in re.split(r"[,/&]|\band\b", concept):
        ws = {w.strip("-'") for w in re.findall(r"[a-z][a-z'-]+", seg)}
        ws = {w for w in ws if len(w) > 2 and w not in GENERIC and w not in MEASURE
              and w not in RESTRICTION}
        if ws:
            segs.append(ws)
    return {"measure": measures, "restriction": restr, "segments": segs}


def clause_tokens(clause: str) -> set[str]:
    return {w.strip("-'") for w in re.findall(r"[a-z][a-z'-]+", clause.lower())}


def _named(w: str, ctoks: set[str], low: str) -> bool:
    if w in ctoks or w in low:
        return True
    for s in SYN.get(w, ()):
        if s in ctoks or s in low:
            return True
    if w.endswith("s") and w[:-1] in ctoks:
        return True
    if w + "s" in ctoks:
        return True
    if len(w) > 5:                                    # stem tolerance
        for c in ctoks:
            if c.startswith(w[:5]) and abs(len(c) - len(w)) <= 3:
                return True
    return False


def prose_vocab(clause_token_sets) -> set[str]:
    """Every word the family's own prose uses, across the sampled editions.

    This calibrates the naming rule to the dataset instead of to a hand-written stop-list. ONS
    series titles are dense with notation that is not a concept -- `PS: Net Borrowing: £m CPNSA`,
    `FA: CG: Liabs: Flows`, `CVM SA`. Requiring the prose to say "cpnsa" guarantees a miss, and
    that is why public sector finances and monthly GDP returned zero channels while CPI (whose
    titles happen to read like English) worked. A token the family's prose NEVER uses cannot be
    the thing the prose names, so it is notation and is dropped from the requirement.
    """
    v = set()
    for s in clause_token_sets:
        v |= s
    return v


def names_series(pt: dict, ctoks: set[str], low: str, vocab: set[str] | None = None
                 ) -> tuple[bool, int]:
    """Does this clause NAME the series in `pt`? -> (ok, specificity).

    MEASURE     cpi / cpih / rpi ... must match EXACTLY when the title carries one. CPI and
                CPIH are different series and `\\bCPI\\b` cannot match inside "CPIH", so
                crediting one to the other is decidable -- and was the #1 false match.
    RESTRICTION excluding / core / contribution / seasonal ... must ALL be named. A title
                saying "Excluding energy" may only be credited to a clause that says so. This
                is what removed 'CPIH 12mth: Excluding energy' and 'RPI - Eggs' from the
                candidate list.
    CONCEPT     DISJUNCTIVE by segment. COICOP titles are comma lists ("HOUSING, WATER,
                ELECTRICITY, GAS AND OTHER FUELS") and prose names one member, so requiring
                every token (the strict-subset first cut) silently deleted every COICOP
                division. At least one whole segment must be named.
    """
    for m in pt["measure"]:
        if not re.search(r"\b" + m + r"\b", low):
            return False, 0
    for r in pt["restriction"]:
        if not _named(r, ctoks, low):
            return False, 0
    # ...and the REVERSE direction, which is where the demo's `disqualifying_terms` list lived.
    # Requiring only that the title's restrictions be named is half a rule: it lets "The CORE
    # CPIH annual inflation rate was 4.1%" be credited to plain `CPIH ANNUAL RATE: ALL ITEMS`,
    # and "the CONTRIBUTION from furniture and household goods was the lowest since..." likewise.
    # That asymmetry produced all 20 surviving superlative "contradictions" in the first full
    # build -- every one of them our error, not the source's. Only the modifiers that denote a
    # genuinely DIFFERENT series are guarded; presentation details ("seasonally adjusted",
    # "annual average") are not, because a guard that fires on wording the title merely omits is
    # a false-negative generator.
    for r in REVERSE_GUARD:
        if r not in pt["restriction"] and _named(r, ctoks, low):
            return False, 0
    spec = len(pt["measure"]) + len(pt["restriction"])
    # Concept tokens the family's prose never uses are notation, not concepts -- see prose_vocab.
    # With notation removed, the requirement can be STRICT again: every surviving concept token
    # must be named. Segment-disjunctive matching ("name any one segment") was necessary only
    # because notation tokens made strict matching impossible, and it is far too weak on
    # datasets whose concept is generic -- `Trade in Goods: Vegetables & fruit (05): WW: Imports`
    # reduced to requiring the single word "goods", so a trade bulletin that never mentions fruit
    # acquired it as a channel. Strict-over-filtered-tokens keeps CPI (whose all-items titles
    # carry no concept tokens at all, only the measure "cpi") and drops the commodity zoo.
    req = set()
    for seg in pt["segments"]:
        req |= {w for w in seg if vocab is None or w in vocab}
    if req:
        got = sum(1 for w in req if _named(w, ctoks, low))
        # Neither all-or-nothing extreme survives real prose. Requiring ANY one token let a trade
        # bulletin acquire a vegetables-and-fruit channel off the single word "goods"; requiring
        # ALL of them rejected `Public sector net borrowing, excluding public sector banks` for a
        # clause reading "borrowing - the difference between total public sector spending and
        # income - was £16.0 billion", which is unmistakably that series to a reader. So the
        # clause must cover most of the concept, and IDENTIFIABILITY (MAX_MULTIPLICITY) plus each
        # channel's own month-shifted control do the discriminating -- both measured, not assumed.
        if got < max(1, int(round(NAME_COVERAGE * len(req)))):
            return False, 0
        spec += got
    elif spec == 0:
        # Nothing requirable survives: no measure, no restriction, and every concept token was
        # notation. Such a title would match ANY clause, so it is not matchable at all.
        return False, 0
    return True, spec


# ============================ value matching =================================================

def unit_scale(u: str) -> float | None:
    """Scale of a series' own unit, or None when the dataset does not say.

    ONS unit strings are terse and inconsistent across datasets -- the public-sector-finances
    CSV labels £-million series as `m`, `M`, or `` (empty), and £-billion series as `bn`. A
    parser that only knew "million"/"£m" read all of those as scale 1, so every "£16.0 billion"
    claim was compared against 16,000,000,000 while the series held 16,000: 794 claims, 0
    matches. None means "unknown", and an unknown unit is allowed to match at any scale rather
    than silently at the wrong one -- the coincidence cost of that is what the control measures.
    """
    u = (u or "").strip().lower()
    if not u:
        return None
    if re.fullmatch(r"£?\s*(bn|billions?)", u) or "billion" in u:
        return 1e9
    if re.fullmatch(r"£?\s*(m|mn|millions?)", u) or "million" in u:
        return 1e6
    if re.fullmatch(r"£?\s*(k|thousands?)", u) or "thousand" in u:
        return 1e3
    if "%" in u or "index" in u or "per" in u:
        return 1.0
    return None


def tol_for(numtext: str) -> float:
    """Half the last shown decimal: '2.8'->0.05, '2.85'->0.005, '3'->0.5.

    Precision-aware, so a figure quoted to one decimal is not matched as if it were exact.
    """
    d = len(numtext.split(".")[1]) if "." in numtext else 0
    return 0.5 * (10 ** -d)


_WEIGHTS_UNIT = re.compile(r"(?i)parts per|per 1000|per thousand")


def unit_compatible(kind: str, ons_unit: str) -> bool:
    """Can a claim of this KIND legitimately be a value of a series in this UNIT?

    A dataset carries the concept's weights alongside its rates -- mm23 holds ~700 "CPI wts:"
    series in "Parts per 1000". Those are numerically in the same range as annual rates, so a
    "2.8%" claim can land on one by coincidence. A percentage claim is not a weight, so the
    unit rules it out even when the number agrees.
    """
    if kind in ("pct", "pp") and _WEIGHTS_UNIT.search(ons_unit or ""):
        return False
    return True


def build_index(series: dict, bucket: str = "m") -> dict:
    """period -> (sorted values, parallel cdids), for range lookup instead of a full scan."""
    by = collections.defaultdict(list)
    for cd, s in series.items():
        for k, v in s.get(bucket, {}).items():
            by[k].append((v, cd))
    out = {}
    for k, pairs in by.items():
        pairs.sort()
        out[k] = ([p[0] for p in pairs], [p[1] for p in pairs])
    return out


def lookup(idx: dict, period: str, lo: float, hi: float) -> list[str]:
    e = idx.get(period)
    if not e:
        return []
    vals, cds = e
    return cds[bisect.bisect_left(vals, lo):bisect.bisect_right(vals, hi)]


# A figure that matches many series identifies none of them. `uktrade` holds 1,567 monthly
# series whose titles all begin "Trade in Goods:", so a clause saying "trade in goods ... 4.0%"
# names and value-matches dozens at once -- which is how "Trade in Goods: Malta: Total: Imports"
# and "Manufacture of Soap & Detergents" became candidate channels for bulletins that never
# mention Malta or soap. Requiring a claim to be IDENTIFYING is the derived form of the guard
# that per-channel specificity only approximated.
MAX_MULTIPLICITY = 3

# Fraction of a series title's requirable concept tokens the clause must name. Calibrated by
# measurement across 8 diverse families -- see README "Channel discovery".
NAME_COVERAGE = 0.8


def probes_for(v: float, kind: str, scale: float, numtext: str):
    """Value windows to search. A '£12.3 billion' claim lands in a series held in £m as 12300."""
    t = tol_for(numtext)
    if kind in ("pct", "pp", "points"):
        return ((v - t, v + t, 1.0),)
    return tuple(((v * scale - t * scale) / us, (v * scale + t * scale) / us, us)
                 for us in (1.0, 1e3, 1e6, 1e9))


# ============================ text chunking ==================================================

def chunk_paragraphs(paras: list[str], cap: int) -> list[tuple[int, int]]:
    """Consecutive [lo, hi) paragraph runs, each within `cap` chars, covering ALL paragraphs.

    The alternative -- keeping only the single densest run per section -- discarded ~70% of
    real first-party prose (measured compression 3.4x). A token cap should SPLIT the source,
    not cut it: truncating a recites record orphans the numbers that follow the cut, and the
    discarded remainder is exactly as real and as quotable as the part kept.
    """
    out, lo = [], 0
    n = len(paras)
    while lo < n:
        hi, size = lo, 0
        while hi < n:
            add = len(paras[hi]) + (2 if hi > lo else 0)
            if size + add > cap and hi > lo:
                break
            size += add
            hi += 1
        if hi == lo:            # a single paragraph longer than the cap
            hi = lo + 1
        out.append((lo, hi))
        lo = hi
    return out


def split_long_paragraph(p: str, cap: int) -> list[str]:
    """Split one over-cap paragraph at SENTENCE boundaries, keeping every sentence.

    18 records in the first full build ran to 2,011 chars (502 tokens) because a single
    paragraph longer than the cap was emitted whole. Truncating it would orphan the numbers past
    the cut -- the reason chunking exists at all -- so the paragraph is divided instead, and
    every sentence still ships. Sentence bounds use the same terminator+capital rule as
    split_clauses, so a decimal point inside "2.8%" is not a sentence end.
    """
    if len(p) <= cap:
        return [p]
    bounds = [0] + [m.end() for m in re.finditer(r'(?<=[.!?])\s+(?=[A-Z"‘“(])', p)] + [len(p)]
    sents = [p[a:b].strip() for a, b in zip(bounds, bounds[1:]) if p[a:b].strip()]
    out, cur = [], ""
    for s in sents:
        if cur and len(cur) + 1 + len(s) > cap:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out or [p[:cap]]


def window(series: dict, ref: str, n: int, bucket: str = "m") -> tuple[list, list]:
    """Trailing n points ENDING at ref. Explicit gaps as None -- never imputed."""
    keys = []
    if bucket == "m":
        y, m = int(ref[:4]), int(ref[5:7])
        for _ in range(n):
            keys.append(f"{y:04d}-{m:02d}")
            m -= 1
            if m == 0:
                y, m = y - 1, 12
    else:
        y, q = int(ref[:4]), int(ref[-1])
        for _ in range(n):
            keys.append(f"{y:04d}-Q{q}")
            q -= 1
            if q == 0:
                y, q = y - 1, 4
    keys.reverse()
    return [series.get(k) for k in keys], keys
