#!/usr/bin/env python3
"""Build CPT world-knowledge records from RBNZ Monetary Policy Statements.

One record = an MPS's own VERBATIM overview narrative paired with the multi-channel macro series
(OCR, unemployment, inflation components, GDP, TWI) it discusses.

ACCESS: rbnz.govt.nz returned HTTP 403 to direct fetches during scouting (2026-08-03) -- reads as
a bot-wall (deep-research5 separately flagged RBNZ's ~1 req/min bot rate limit). This build sources
from Wayback Machine snapshots instead, which are not behind that wall.

THE "CURRENT QUARTER" SPLIT: the data pack's own-vintage column ("Feb MPS" etc.) runs from measured
history straight into the Bank's own projection with no visual break. Verified against the
Feb-2026 MPS: OCR's row for the statement's own quarter (Q1 2026) = 2.25%, exactly the announced
decision -- a real current decision, not a forecast. But unemployment/inflation's same-quarter row
IS a genuine forward projection; the text's quoted values match the PRIOR quarter (last real
outturn) instead. All quarterly channels are windowed to the same end-quarter (schema requires
equal length per freq); where that final point is itself a projection, it's handled under the same
forecast-not-measured caveat as WASDE #41 / GAIN #58.

Usage:
    python scripts/build_cpt_jsonl.py --config config.example.yaml
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import openpyxl
import yaml

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent
sys.path.insert(0, str(PKG_ROOT.parent / "schema"))
from emit import emit_record  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def wayback_url(url: str, ts: str) -> str:
    return f"http://web.archive.org/web/{ts}/{url}"


class WaybackPlaceholderError(RuntimeError):
    """Wayback served its own HTML wrapper instead of the archived binary. Confirmed real and
    non-deterministic 2026-08-03: the SAME xlsx URL returned a valid 1.97MB file once, then a
    10KB HTML placeholder on the next fetch with no code change -- Wayback's nearest-capture
    redirect can land on a different, uncaptured timestamp. Retry rather than trust the first
    response for binary assets."""


def fetch(url: str, cache: Path, expect_zip: bool = False, retries: int = 3) -> bytes:
    if cache.exists():
        return cache.read_bytes()
    cache.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=120) as r:
            blob = r.read()
        if expect_zip and blob[:2] != b"PK":
            last = WaybackPlaceholderError(
                f"expected an .xlsx (zip) but got {len(blob)} bytes starting {blob[:40]!r} "
                f"-- Wayback placeholder, retrying ({attempt+1}/{retries})")
            continue
        cache.write_bytes(blob)
        return blob
    raise last


def html_to_text(html: bytes) -> str:
    t = html.decode("utf-8", "ignore")
    t = re.sub(r"<script.*?</script>", " ", t, flags=re.S)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&rsquo;", "’")
    return re.sub(r"\s+", " ", t).strip()


_NAV_JUNK = (
    re.compile(r"Download the MPS \(PDF,\s*\d+\s*MB\)\s*"),
    re.compile(r"Read the MPS online\s*"),
)


def extract_narrative(html: bytes, tcfg: dict) -> str:
    text = html_to_text(html)
    start_m = re.search(r"Latest OCR decision", text)
    if not start_m:
        return ""
    end_m = re.search(r"Most recent outlook for the OCR", text[start_m.end():])
    end = start_m.end() + end_m.start() if end_m else len(text)
    body = text[start_m.end():end].strip()
    for pat in _NAV_JUNK:
        body = pat.sub("", body)
    return re.sub(r"\s+", " ", body).strip()


# ---------------------------------------------------------------------------- XLSX parsing
def find_vintage_col(header_row: tuple, quarter_label: str) -> int:
    for i, v in enumerate(header_row):
        if v and str(v).strip() == quarter_label:
            return i
    raise ValueError(f"vintage column {quarter_label!r} not found in {header_row!r}")


def parse_date(s):
    return datetime.strptime(s, "%d/%m/%Y") if isinstance(s, str) else s


def load_sheet_series(wb, sheet_name: str, quarter_label: str) -> tuple[list, dict]:
    """Return (rows) as [(date, {col_name: value})], date-col auto-detected (first col with real
    dd/mm/yyyy strings), values taken only from the release's OWN vintage column onward from the
    header row that names it (falls back to ALL non-date columns for single-vintage sheets like
    TWI, which have no 'Feb MPS'-style column split)."""
    ws = wb[sheet_name]
    all_rows = list(ws.iter_rows(values_only=True))
    date_col = None
    first_date_row = None
    for i, row in enumerate(all_rows[:15]):
        for c, v in enumerate(row):
            if isinstance(v, str) and re.match(r"^\d{2}/\d{2}/\d{4}$", v):
                date_col = c
                first_date_row = i
                break
        if date_col is not None:
            break
    if date_col is None:
        return [], {}

    # Layout is consistent across every sheet checked (i.1, 2.11, 2.15, 2.1, 3.10): a LABEL row
    # (vintage names "Feb MPS"/"Nov MPS", or named sub-series "Headline"/"Non-tradables"/
    # "Tradables") two rows above the first date row, then a UNITS row ("%", "Index", ...)
    # immediately above it. Do NOT use the units row as the label row (that was the original bug
    # here -- it silently fed "%"/"%" in as if they were distinct column names).
    header_row_idx = max(0, first_date_row - 2)
    header = all_rows[header_row_idx]
    labels = {c: str(v).strip() for c, v in enumerate(header) if v}
    try:
        vcol = find_vintage_col(header, quarter_label)
        value_cols = {vcol: labels.get(vcol, "value")}
    except ValueError:
        # single-vintage sheet (e.g. TWI) -- take whatever columns exist right after the date col
        value_cols = {c: lbl for c, lbl in labels.items() if c > date_col} or {date_col + 1: "value"}

    out = []
    for row in all_rows:
        d = row[date_col] if date_col < len(row) else None
        if not (isinstance(d, str) and re.match(r"^\d{2}/\d{2}/\d{4}$", d)):
            continue
        vals = {}
        for c, name in value_cols.items():
            if c < len(row) and isinstance(row[c], (int, float)):
                vals[name] = float(row[c])
        if vals:
            out.append((parse_date(d), vals))
    return out, value_cols


def window_ending_at(rows: list, end_date, n: int) -> list:
    idx = next((i for i, (d, _) in enumerate(rows) if d == end_date), None)
    if idx is None:
        return []
    return rows[max(0, idx - n + 1): idx + 1]


# ---------------------------------------------------------------------------- alignment
_SUPERLATIVE = re.compile(r"\b(highest|largest|record[- ]high|all-time high|lowest|smallest|"
                          r"record[- ]low|all-time low)\b", re.I)


# A number matching a channel's value is not enough evidence on its own -- caught during the
# build: the text's "Inflation increased to 3.1%" was credited to `ocr_pct` purely because OCR
# ALSO happened to sit at 3.14 (-> "3.1") two quarters earlier, a coincidental cross-channel
# collision (same failure shape as GAIN's unit-word check). Require one of the channel's own
# keywords within 60 chars BEFORE the matched number.
_CHANNEL_KEYWORDS = {
    "ocr_pct": ("OCR", "Official Cash Rate", "cash rate"),
    "unemployment_rate_pct": ("unemployment",),
    "inflation_headline": ("inflation",),  # "headline" is rarely said outright -- bare "Inflation" IS headline
    "inflation_non_tradables": ("non-tradables inflation", "non-tradables"),
    "production_gdp_real_2009_10_nzd_bn": ("GDP", "economic growth", "economic activity"),
    "twi_index": ("TWI", "trade-weighted", "exchange rate"),
}
# "tradables inflation" is a literal substring of "non-tradables inflation" -- a plain membership
# check would let a non-tradables sentence satisfy the tradables channel too. Use a negative
# lookbehind instead of a keyword tuple for this one channel.
_TRADABLES_RE = re.compile(r"(?<!non-)(?<!non )tradables inflation", re.I)


def detect_alignment(text: str, channel_points: dict) -> tuple[str, list]:
    """channel_points: {channel_name: [(date, value), ...]} (trailing window, oldest-first).
    Checks the last few points of each channel (not just the final one -- the statement's own
    quarter may itself be the Bank's forecast, while the text quotes the prior actual quarter)."""
    evidence = []
    for name, pts in channel_points.items():
        keywords = _CHANNEL_KEYWORDS.get(name, ())
        for date, val in pts[-3:]:
            for form in (f"{val:g}", f"{val:.1f}"):
                if len(form.replace(".", "").replace("-", "")) < 2:
                    continue
                for m in re.finditer(re.escape(form) + r"%", text):
                    before = text[max(0, m.start() - 60): m.start()]
                    if name == "inflation_tradables":
                        if not _TRADABLES_RE.search(before):
                            continue
                    elif keywords and not any(k.lower() in before.lower() for k in keywords):
                        continue
                    evidence.append({"channel": name, "date": date.strftime("%Y-%m-%d"),
                                     "value": val, "quoted_as": form + "%"})
                    break
                else:
                    continue
                break
    alignment = "recites" if evidence else "describes"
    return alignment, evidence


def check_superlatives(text: str, channel_points: dict) -> list:
    flags = []
    for m in _SUPERLATIVE.finditer(text):
        window = text[max(0, m.start() - 80): m.end() + 100]
        num = re.search(r"(\d+\.?\d*)%", window)
        if not num:
            continue
        val = float(num.group(1))
        for name, pts in channel_points.items():
            vals = [v for _, v in pts]
            if not vals or abs(val - vals[-1]) > 5:
                continue
            is_high = bool(re.search(r"highest|largest|record[- ]high|all-time high", m.group(0), re.I))
            extreme = max(vals) if is_high else min(vals)
            if abs(extreme - val) > 1e-6:
                flags.append({"channel": name, "claim": m.group(0), "claimed_value": val,
                             "actual_extreme": extreme})
    return flags


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(PKG_ROOT / "config.example.yaml"))
    ap.add_argument("--set", action="append", default=[])
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    for override in args.set:
        k, v = override.split("=", 1)
        d = cfg
        parts = k.split(".")
        for p in parts[:-1]:
            d = d[p]
        d[parts[-1]] = yaml.safe_load(v)

    cache = PKG_ROOT / cfg["data"]["cache_dir"]
    scfg, tcfg, lcfg = cfg["series"], cfg["text"], cfg["license"]
    stats = {"statements": 0, "emitted": 0, "recites": 0, "describes": 0, "no_narrative": 0,
             "short_series": 0, "superlative_flags": 0, "superlative_dropped": 0}
    records = []

    for st in cfg["statements"]:
        stats["statements"] += 1
        page_html = fetch(wayback_url(st["page_url"], st["page_wayback_ts"]),
                          cache / "pages" / f"{st['published']}.html")
        text = extract_narrative(page_html, tcfg)
        if len(text) < tcfg["min_chars"]:
            stats["no_narrative"] += 1
            continue

        xlsx_blob_path = cache / "xlsx" / f"{st['published']}.xlsx"
        fetch(wayback_url(st["data_xlsx_url"], st["data_xlsx_wayback_ts"]), xlsx_blob_path,
              expect_zip=True)
        wb = openpyxl.load_workbook(xlsx_blob_path, data_only=True, read_only=True)

        end_q = parse_date(st["current_quarter_date"])
        channel_points, meta_channels = {}, []
        ok = True
        for sheet_name, scfg_sheet in scfg["sheets"].items():
            if sheet_name not in wb.sheetnames:
                continue
            rows, value_cols = load_sheet_series(wb, sheet_name, st["quarter_label"])
            if not rows:
                continue
            is_daily = scfg_sheet.get("daily")
            if is_daily:
                window = rows[-scfg["daily_window"]:]
            else:
                window = window_ending_at(rows, end_q, scfg["quarterly_window"])
            if len(window) < scfg["min_points"]:
                continue
            for col_name in (scfg_sheet.get("cols") or [None]):
                key = col_name or list(value_cols.values())[0]
                pts = [(d, v[key]) for d, v in window if key in v]
                if len(pts) < scfg["min_points"]:
                    continue
                unit = (f"{scfg_sheet.get('unit_prefix','')}_{key}".strip("_").lower()
                        if scfg_sheet.get("unit_prefix") else scfg_sheet["unit"])
                unit = re.sub(r"[^a-z0-9]+", "_", unit.lower())
                channel_points[unit] = pts
                meta_channels.append({"unit": unit, "sheet": sheet_name,
                                      "n_points": len(pts), "daily": bool(is_daily)})
        if len(channel_points) < 2:
            stats["short_series"] += 1
            continue

        alignment, evidence = detect_alignment(text, channel_points)
        superlative_flags = check_superlatives(text, channel_points)
        stats["superlative_flags"] += len(superlative_flags)
        if superlative_flags and tcfg.get("drop_on_superlative_contradiction", True):
            stats["superlative_dropped"] += 1
            continue

        ts = [{"values": [v for _, v in pts], "unit": name,
               "freq": scfg["daily_freq"] if any(m["unit"] == name and m["daily"] for m in meta_channels)
               else scfg["quarterly_freq"]}
              for name, pts in channel_points.items()]
        first_date = min(pts[0][0] for pts in channel_points.values())
        last_date = max(pts[-1][0] for pts in channel_points.values())
        rec = emit_record(
            text=text + "\n\n<ts></ts>",
            timeseries=ts,
            alignment=alignment,
            license=lcfg["tag"],
            source=st["page_url"],
            series_id=f"rbnz_mps_{st['published']}",
            dataset="rbnz_mps",
            domain="monetary_policy",
            region=st["region"],
            period_start=first_date.strftime("%Y-%m-%d"),
            period_end=last_date.strftime("%Y-%m-%d"),
            meta={
                "published": st["published"],
                "quarter_label": st["quarter_label"],
                "current_quarter": st["current_quarter_date"],
                "channels": meta_channels,
                "n_channels": len(channel_points),
                "recite_evidence": evidence,
                "superlative_flags": superlative_flags,
                "true_license": lcfg["true_license"],
                "attribution_required": lcfg["attribution_required"],
                "access_method": "wayback_snapshot",
                "series_note": (
                    "quarterly channels windowed to the statement's own quarter (OCR = a real "
                    "current decision there; unemployment/inflation's same-quarter point is the "
                    "Bank's own forecast -- forecast-not-measured caveat, same convention as "
                    "WASDE #41 / GAIN #58). TWI is daily, its own trailing window."),
            },
        )
        records.append(rec)
        stats["emitted"] += 1
        stats[alignment] += 1

    out = PKG_ROOT / cfg["output"]["path"]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    rr = PKG_ROOT / cfg["output"]["run_report"]
    rr.write_text(json.dumps({"dataset": "rbnz_mps", "stats": stats, "config_snapshot": cfg},
                              indent=2, ensure_ascii=False))
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"\nwrote {len(records)} records -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
