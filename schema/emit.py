#!/usr/bin/env python3
"""emit_record() — construct a v1-compliant CPT record that passes `validate.py --strict`.

Producer-side counterpart to `validate.py` (the checker). Build scripts call this to build
each record, so records are *born* schema-clean instead of being migrated afterward.

Single source of truth: the controlled vocab (`ALIGNMENT`, `LICENSE`, `TEXT_SOURCE`,
`TEXT_QUALITY`, `URL_RE`) and the record checker (`validate_record`) are imported from the
sibling `validate.py`, so this helper can never drift from the gate it targets. Every record
is self-checked in strict mode before it is returned — a misconfigured build fails loudly at
construction time with a clear message, rather than silently emitting junk.

Usage (from a package build script):

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "schema"))
    from emit import emit_record

    rec = emit_record(
        text=text,
        timeseries=[{"values": vals, "unit": "stage_ft", "freq": "1h"}],
        alignment="describes",
        license="public-domain-us-gov",
        source="https://api.water.noaa.gov/nwps/v1/gauges/CCNO1",
        dataset="noaa_nwps_flood",
        series_id="nwps_CCNO1_20250407T21",
        domain="hydrology", region="US-OH",
        period_start="2025-03-28", period_end="2025-04-17",
        timestamps=timestamps,
        meta={"gauge_lid": "CCNO1", "crest_ft": 60.94},
    )
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Import the vocab + checker from the sibling validator (single source of truth).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate import (  # noqa: E402
    validate_record,
    ALIGNMENT,
    LICENSE,
    TEXT_SOURCE,
    TEXT_QUALITY,
    URL_RE,
)

# Standardized optional fields, in the order we want them to appear after the required four.
_OPTIONAL_ORDER = (
    "series_id", "dataset", "source", "license", "text_source",
    "alignment", "domain", "region", "period_start", "period_end",
)


def emit_record(
    *,
    text: str,
    timeseries: List[Dict[str, Any]],
    alignment: str,
    license: str,
    source: Optional[str] = None,
    text_quality: str = "real",
    text_source: str = "first_party_official",
    series_id: Optional[str] = None,
    dataset: Optional[str] = None,
    domain: Optional[str] = None,
    region: Optional[str] = None,
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    timestamps: Optional[List[str]] = None,
    meta: Optional[Dict[str, Any]] = None,
    multi_series: bool = False,
    strict: bool = True,
) -> Dict[str, Any]:
    """Build a v1 CPT record guaranteed to pass validate.py (strict by default).

    Required: text, timeseries, alignment, license. `alignment`/`license`/`text_source`/
    `text_quality` are checked against the controlled vocab up front (clear ValueError on a
    typo). `source`, if given, must be a URL. Dataset-specific keys go in `meta`.
    """
    # --- controlled-vocab guards (fail loudly with the allowed set) ---
    if alignment not in ALIGNMENT:
        raise ValueError(f"alignment {alignment!r} not in {sorted(ALIGNMENT)}")
    if license not in LICENSE:
        raise ValueError(f"license {license!r} not in {sorted(LICENSE)}")
    if text_source not in TEXT_SOURCE:
        raise ValueError(f"text_source {text_source!r} not in {sorted(TEXT_SOURCE)}")
    if text_quality not in TEXT_QUALITY:
        raise ValueError(f"text_quality {text_quality!r} not in {sorted(TEXT_QUALITY)}")
    if source is not None and not URL_RE.match(str(source)):
        raise ValueError(f"source must be a canonical URL, got {source!r}")

    rec: Dict[str, Any] = {
        "text": text,
        "timeseries": timeseries,
        "task_type": "world_knowledge",
        "text_quality": text_quality,
    }
    if multi_series:
        rec["multi_series"] = True

    values = {
        "series_id": series_id, "dataset": dataset, "source": source,
        "license": license, "text_source": text_source, "alignment": alignment,
        "domain": domain, "region": region,
        "period_start": period_start, "period_end": period_end,
    }
    for k in _OPTIONAL_ORDER:
        if values[k] is not None:
            rec[k] = values[k]

    if timestamps is not None:
        rec["timestamps"] = timestamps
    if meta:
        rec["meta"] = meta

    # --- self-check: must pass the same gate as validate.py (strict promotes warns) ---
    errs, warns = validate_record(rec, min_text_chars=1)
    problems = errs + warns if strict else errs
    if problems:
        raise ValueError(
            "emit_record built a record that fails validate.py"
            + (" --strict" if strict else "")
            + ":\n  - " + "\n  - ".join(problems)
        )
    return rec
