#!/usr/bin/env python3
"""Parse cached USDM summary XML: sections, region mapping, author bylines."""
from __future__ import annotations

import html
import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
XML_DIR = PKG / ".cache" / "summary_xml"

# The six official USDM regions == the six RegionalClimateCenterStatistics aoi codes.
REGION_AOI = {
    "High Plains": "1",
    "Midwest": "2",
    "Northeast": "3",
    "South": "4",
    "Southeast": "5",
    "West": "6",
}

# Non-CONUS sections. They have narrative prose but no CONUS-region series, so
# they are never emitted as regional records.
NON_CONUS = {
    "caribbean", "pacific", "alaska", "hawaii", "puerto rico",
    "hawaii, alaska and puerto rico", "hawaii, alaska, and puerto rico",
    "virgin islands", "alaska and hawaii", "u.s. affiliated pacific islands",
}


def norm_label(name: str) -> str:
    """Normalize a <region name> for matching: casefold, drop a leading 'The '."""
    s = unicodedata.normalize("NFKC", html.unescape(name or "")).strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"^the\s+", "", s, flags=re.I)
    return s.casefold().strip(" .:")


# A section maps to a series ONLY when its label is the official region's own
# name (optionally suffixed "Region", and with a leading "The " already
# stripped by norm_label). Merged or descriptive labels -- "The Great Plains to
# Midwest", "The Western States", "The East" -- are deliberately NOT mapped:
# their footprint is not the RCC polygon the series measures, so pairing them
# would weaken alignment for a handful of records. Those sections are dropped;
# the week still contributes its national record.
REGION_ALIAS = {
    name.casefold(): name for name in REGION_AOI
}
REGION_ALIAS.update({f"{name.casefold()} region": name for name in REGION_AOI})


def map_region(name: str):
    """Return the official region for a section label, or None."""
    return REGION_ALIAS.get(norm_label(name))


def is_non_conus(name: str) -> bool:
    return norm_label(name) in NON_CONUS


def _text_of(el) -> str:
    """Flatten an element's <p> children into paragraph text."""
    paras = []
    for p in el.findall("p"):
        t = "".join(p.itertext())
        t = re.sub(r"\s+", " ", html.unescape(t)).strip()
        if t:
            paras.append(t)
    if not paras:
        t = re.sub(r"\s+", " ", "".join(el.itertext())).strip()
        if t:
            paras = [t]
    return "\n\n".join(paras)


def parse_week(date8: str) -> dict | None:
    """Parse one cached weekly XML into sections + authors."""
    path = XML_DIR / f"{date8}.xml"
    if not path.exists():
        return None
    raw = path.read_bytes()
    # Files are served as utf-16 or utf-8 depending on era; sniff.
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            txt = raw.decode(enc)
            if "<Results" in txt:
                break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        return None
    txt = re.sub(r"^\s*<\?xml[^>]*\?>", "", txt).strip()
    # The feed is not always well-formed. Two real defects, both in prose:
    #   - bare "&"
    #   - "<" used as less-than: "(<25th percentile)", "(<0.5 inches)", "(<5%)"
    txt = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9A-Fa-f]+);)", "&amp;", txt)
    txt = re.sub(r"<(?![/!?a-zA-Z])", "&lt;", txt)
    try:
        root = ET.fromstring(txt)
    except ET.ParseError:
        return None
    week = root.find("week")
    if week is None:
        return None

    out = {
        "date8": date8,
        "release_date": (week.findtext("date") or "").strip(),
        "intro": "",
        "forecast": "",
        "regions": [],       # (raw_label, mapped_or_None, text)
        "authors": [],       # (name, affiliation)
    }
    intro = week.find("intro")
    if intro is not None:
        out["intro"] = _text_of(intro)
    fc = week.find("forecast")
    if fc is not None:
        out["forecast"] = _text_of(fc)
    for r in week.findall("region"):
        label = r.get("name") or ""
        out["regions"].append((label, map_region(label), _text_of(r)))
    for a in week.findall("author"):
        out["authors"].append(((a.findtext("name") or "").strip(),
                               (a.findtext("affiliation") or "").strip()))
    return out


# --- byline -> federal / non-federal, for the per-record license split ---------
FEDERAL_PAT = re.compile(
    r"\b(noaa|nws|national weather service|nchs|"
    r"national (?:centers|climatic|centers for environmental)|ncei|ncdc|"
    r"climate prediction center|\bcpc\b|nesdis|"
    r"u\.?s\.? department of agriculture|usda|"
    r"department of commerce|usace|army corps|"
    r"u\.?s\.? geological survey|usgs|"
    r"bureau of reclamation|nidis|noaa/)\b", re.I)
NONFED_PAT = re.compile(
    r"\b(ndmc|national drought mitigation center|"
    r"university|univ\.|unl\b|nebraska|"
    r"dri\b|desert research institute|wrcc|western regional climate center|"
    r"hprcc|mrcc|sercc|srcc|nrcc|cirrus|"
    r"regional climate cent)", re.I)


def classify_affiliation(aff: str) -> str:
    """'federal' | 'nonfederal' | 'unknown' for one affiliation string."""
    a = aff or ""
    # Check non-federal first: "WRCC, DRI" and "NDMC" must never read as federal,
    # and some strings name both (e.g. a university hosting a NOAA center).
    if NONFED_PAT.search(a):
        return "nonfederal"
    if FEDERAL_PAT.search(a):
        return "federal"
    return "unknown"


def week_license(authors) -> tuple[str, str]:
    """Per-record license from the week's byline.

    **Attribution rule: the FIRST listed author owns the week.** The XML lists
    1-3 authors and never says who wrote which paragraph; the USDM credits the
    lead author with the week's map and summary, so the lead byline is taken as
    authoritative for the whole release. This is a licensing-owner decision
    (Defu, 2026-07-25), not something the source states -- a reviewer who reads
    each week as a joint work would instead quarantine every co-authored week.
    `byline_class` and the full `authors` list are kept in each record's meta so
    the call can be re-litigated without a rebuild.
    """
    if not authors:
        return "unknown", "no-byline"
    kind = classify_affiliation(authors[0][1])
    if kind == "federal":
        return "public-domain-us-gov", "first-author-federal"
    if kind == "nonfederal":
        return "proprietary-review", "first-author-nonfederal"
    return "unknown", "unrecognized-affiliation"
