#!/usr/bin/env python3
"""Validate CPT world-knowledge JSONL records against schema v1.

Checks both JSON-Schema-expressible constraints and the semantic rules
(single <ts>, channel-length equality, freq token, enum membership) that a
plain schema can't express. See schema/SCHEMA.md.

Two severities:
  * ERROR   - violates the required v1 contract (the 4 required fields, <ts>
              count, channel shape, freq token). Always fails the file.
  * WARNING - optional field uses a value outside the recommended v1 vocab
              (e.g. free-form `source`/`license`/`text_source`). Existing
              packages predate the standardized vocab and are allowed through
              the freeze; new packages should clear warnings. Use --strict to
              promote warnings to errors.

Usage:
    python validate.py FILE_OR_DIR [FILE_OR_DIR ...] [--min-text-chars N]
                       [--max-report N] [--quiet] [--strict]

Exit code 0 if every record passes (errors only, unless --strict), 1 otherwise.
No third-party deps.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

FREQ_RE = re.compile(r"^\d+(ms|m|h|d|w|W|M|q|y|over)$")
URL_RE = re.compile(r"^https?://", re.IGNORECASE)
TS_TOKEN = "<ts></ts>"

TASK_TYPES = {"world_knowledge"}
TEXT_QUALITY = {"real", "generated"}
TEXT_SOURCE = {"first_party_official", "first_party_human", "third_party", "generated"}
ALIGNMENT = {"recites", "describes", "contextualizes"}
LICENSE = {"public-domain-us-gov", "cc-by-4.0", "cc0", "proprietary-review", "unknown"}


def validate_record(rec, min_text_chars: int):
    """Return (errors, warnings) for one record. errors == [] means the
    required v1 contract holds; warnings flag optional-vocab drift."""
    errs: list[str] = []
    warns: list[str] = []

    if not isinstance(rec, dict):
        return ["record is not a JSON object"], []

    # --- required: text ---
    text = rec.get("text")
    if not isinstance(text, str) or not text:
        errs.append("`text` missing or not a non-empty string")
        text = ""
    elif len(text) < min_text_chars:
        errs.append(f"`text` shorter than min_text_chars ({len(text)} < {min_text_chars})")

    # --- required: timeseries ---
    ts = rec.get("timeseries")
    channel_len = None
    if not isinstance(ts, list) or len(ts) == 0:
        errs.append("`timeseries` missing or empty")
        ts = []
    else:
        seen_units = set()
        lengths = []
        len_by_freq = {}  # freq -> set of lengths (same-freq channels should match)
        for i, ch in enumerate(ts):
            if not isinstance(ch, dict):
                errs.append(f"timeseries[{i}] is not an object")
                continue
            vals = ch.get("values")
            vlen = None
            if not isinstance(vals, list) or len(vals) == 0:
                errs.append(f"timeseries[{i}].values missing or empty")
            else:
                if not all(v is None or isinstance(v, (int, float)) for v in vals):
                    errs.append(f"timeseries[{i}].values has non-numeric entries")
                vlen = len(vals)
                lengths.append(vlen)
            unit = ch.get("unit")
            if not isinstance(unit, str) or not unit:
                errs.append(f"timeseries[{i}].unit missing or empty")
            else:
                # units may legitimately repeat (a physical unit, not a key);
                # distinctness is a recommendation, not a requirement.
                if unit in seen_units:
                    warns.append(f"timeseries[{i}].unit repeats {unit!r} (recommend distinct channel labels)")
                seen_units.add(unit)
            freq = ch.get("freq")
            if not isinstance(freq, str) or not FREQ_RE.match(freq):
                errs.append(f"timeseries[{i}].freq invalid: {freq!r}")
            elif vlen is not None:
                len_by_freq.setdefault(freq, set()).add(vlen)
        # Channels MAY have different lengths (mixed-frequency records are
        # legitimate). But channels sharing a freq should share a length.
        for freq, lens in len_by_freq.items():
            if len(lens) > 1:
                errs.append(f"channels at freq {freq!r} have unequal lengths {sorted(lens)}")
        # Reference length for timestamps checks: unique length, or the
        # single-freq length; None when genuinely mixed.
        if len(set(lengths)) == 1:
            channel_len = lengths[0]
        elif len(len_by_freq) == 1:
            channel_len = next(iter(next(iter(len_by_freq.values()))))

    # --- required: task_type ---
    if rec.get("task_type") not in TASK_TYPES:
        errs.append(f"`task_type` must be 'world_knowledge', got {rec.get('task_type')!r}")

    # --- required: text_quality ---
    if rec.get("text_quality") not in TEXT_QUALITY:
        errs.append(f"`text_quality` must be in {sorted(TEXT_QUALITY)}, got {rec.get('text_quality')!r}")

    # --- <ts> count ---
    n_ts = text.count(TS_TOKEN)
    if rec.get("multi_series"):
        if n_ts != len(ts):
            errs.append(f"multi_series: <ts> count {n_ts} != len(timeseries) {len(ts)}")
    else:
        if n_ts != 1:
            errs.append(f"expected exactly one <ts></ts>, found {n_ts}")

    # --- optional vocab (warnings: recommended v1 vocab, not required) ---
    if "text_source" in rec and rec["text_source"] not in TEXT_SOURCE:
        warns.append(f"`text_source` outside recommended vocab: {rec['text_source']!r}")
    if "alignment" in rec and rec["alignment"] not in ALIGNMENT:
        warns.append(f"`alignment` outside recommended vocab: {rec['alignment']!r}")
    if "license" in rec and rec["license"] not in LICENSE:
        warns.append(f"`license` outside recommended vocab: {rec['license']!r}")
    if "source" in rec and isinstance(rec["source"], str) and rec["source"] and not URL_RE.match(rec["source"]):
        warns.append(f"`source` is not a URL (recommend canonical URL): {rec['source']!r}")

    # --- timestamps parallelism (structural) ---
    if "timestamps" in rec:
        tstamps = rec["timestamps"]
        if not isinstance(tstamps, list):
            errs.append("`timestamps` is not an array")
        elif channel_len is not None:
            if len(tstamps) != channel_len:
                errs.append(f"`timestamps` length {len(tstamps)} != channel length {channel_len}")
        elif isinstance(ts, list) and lengths and len(tstamps) not in lengths:
            # mixed-frequency record: timestamps must match at least one channel
            warns.append(f"`timestamps` length {len(tstamps)} matches no channel length {sorted(set(lengths))}")

    return errs, warns


def iter_jsonl_files(paths):
    for p in paths:
        if os.path.isdir(p):
            yield from sorted(glob.glob(os.path.join(p, "**", "*.jsonl"), recursive=True))
        elif os.path.isfile(p):
            yield p
        else:
            matched = sorted(glob.glob(p))
            if matched:
                yield from matched
            else:
                print(f"WARN: no such path: {p}", file=sys.stderr)


def validate_file(path, min_text_chars, max_report, quiet, strict):
    total = passed = 0
    failures = []       # (lineno, errs)
    warned = []         # (lineno, warns)
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                failures.append((lineno, [f"invalid JSON: {e}"]))
                continue
            errs, warns = validate_record(rec, min_text_chars)
            if strict:
                errs = errs + warns
                warns = []
            if warns:
                warned.append((lineno, warns))
            if errs:
                failures.append((lineno, errs))
            else:
                passed += 1

    status = "OK " if not failures else "FAIL"
    if not quiet or failures or warned:
        wtxt = f", {len(warned)} with warnings" if warned else ""
        print(f"[{status}] {path}: {passed}/{total} passed, {len(failures)} failed{wtxt}")
    for lineno, errs in failures[:max_report]:
        for e in errs:
            print(f"    ERROR line {lineno}: {e}")
    if len(failures) > max_report:
        print(f"    ... and {len(failures) - max_report} more failing records")
    # collapse warnings to unique messages so output stays short
    if warned and not quiet:
        uniq = {}
        for _, warns in warned:
            for w in warns:
                uniq[w] = uniq.get(w, 0) + 1
        for w, n in list(uniq.items())[:max_report]:
            print(f"    WARN ({n}x): {w}")
    return total, passed, len(failures), len(warned)


def main():
    ap = argparse.ArgumentParser(description="Validate CPT world-knowledge JSONL records.")
    ap.add_argument("paths", nargs="+", help="JSONL files, directories, or globs")
    ap.add_argument("--min-text-chars", type=int, default=1)
    ap.add_argument("--max-report", type=int, default=10, help="max failing records to print per file")
    ap.add_argument("--quiet", action="store_true", help="only print files with failures/warnings")
    ap.add_argument("--strict", action="store_true", help="promote optional-vocab warnings to errors (use for new packages)")
    args = ap.parse_args()

    files = list(iter_jsonl_files(args.paths))
    if not files:
        print("No .jsonl files found.", file=sys.stderr)
        return 2

    g_total = g_passed = g_failed = g_warned = 0
    for path in files:
        t, p, f, w = validate_file(path, args.min_text_chars, args.max_report, args.quiet, args.strict)
        g_total += t
        g_passed += p
        g_failed += f
        g_warned += w

    print(f"\nTOTAL: {g_passed}/{g_total} records passed across {len(files)} file(s), "
          f"{g_failed} failed, {g_warned} with warnings"
          f"{' (strict)' if args.strict else ''}.")
    return 0 if g_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
