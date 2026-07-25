#!/usr/bin/env python3
"""The CPI series each release paragraph is about, plus the API loader.

Keys are stable channel names; values are (BLS series id, human label used in the
`<ts>` intro sentence). Every id here was probed against api.bls.gov v1 and returns
data — see `.cache/probe/probe_exist_*.json`.

`CUSR0000*` = CPI-U, U.S. city average, seasonally adjusted (the basis the release
narrates month-to-month). `CUUR0000*` = not seasonally adjusted (the basis the
release recites index *levels* on, and the only one BLS never revises).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

API_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"

# channel key -> (series id, label for the ts-intro sentence)
SERIES: Dict[str, Tuple[str, str]] = {
    # headline
    "all_items_sa":        ("CUSR0000SA0",     "all items"),
    "all_items_nsa":       ("CUUR0000SA0",     "all items"),
    "core_sa":             ("CUSR0000SA0L1E",  "all items less food and energy"),
    "food_sa":             ("CUSR0000SAF1",    "food"),
    "energy_sa":           ("CUSR0000SA0E",    "energy"),
    "shelter_sa":          ("CUSR0000SAH1",    "shelter"),
    "cpi_w_nsa":           ("CWUR0000SA0",     "CPI-W all items"),
    "cpi_w_sa":            ("CWSR0000SA0",     "CPI-W all items"),
    "c_cpi_u_nsa":         ("SUUR0000SA0",     "C-CPI-U all items"),
    # food
    "food_beverages_sa":   ("CUSR0000SAF",     "food and beverages"),
    "food_at_home_sa":     ("CUSR0000SAF11",   "food at home"),
    "food_away_sa":        ("CUSR0000SEFV",    "food away from home"),
    "cereals_bakery_sa":   ("CUSR0000SAF111",  "cereals and bakery products"),
    "meats_eggs_sa":       ("CUSR0000SAF112",  "meats, poultry, fish, and eggs"),
    "dairy_sa":            ("CUSR0000SEFJ",    "dairy and related products"),
    "fruits_veg_sa":       ("CUSR0000SAF113",  "fruits and vegetables"),
    "nonalc_bev_sa":       ("CUSR0000SAF114",  "nonalcoholic beverages and beverage materials"),
    "other_food_home_sa":  ("CUSR0000SAF115",  "other food at home"),
    "alcoholic_bev_sa":    ("CUSR0000SAF116",  "alcoholic beverages"),
    "full_svc_meals_sa":   ("CUSR0000SEFV01",  "full service meals and snacks"),
    # energy
    "gasoline_sa":         ("CUSR0000SETB01",  "gasoline (all types)"),
    "electricity_sa":      ("CUSR0000SEHF01",  "electricity"),
    "utility_gas_sa":      ("CUSR0000SEHF02",  "utility (piped) gas service"),
    "fuel_oil_sa":         ("CUSR0000SEHE01",  "fuel oil"),
    # housing / shelter
    "housing_sa":          ("CUSR0000SAH",     "housing"),
    "rent_sa":             ("CUSR0000SEHA",    "rent of primary residence"),
    "oer_sa":              ("CUSR0000SEHC",    "owners' equivalent rent of residences"),
    "lodging_away_sa":     ("CUSR0000SEHB",    "lodging away from home"),
    "fuels_utilities_sa":  ("CUSR0000SAH2",    "fuels and utilities"),
    "household_ops_sa":    ("CUSR0000SAH3",    "household furnishings and operations"),
    # apparel
    "apparel_sa":          ("CUSR0000SAA",     "apparel"),
    # transportation
    "transportation_sa":   ("CUSR0000SAT",     "transportation"),
    "public_transp_sa":    ("CUSR0000SETG",    "public transportation"),
    "new_vehicles_sa":     ("CUSR0000SETA01",  "new vehicles"),
    "used_cars_sa":        ("CUSR0000SETA02",  "used cars and trucks"),
    "airline_fares_sa":    ("CUSR0000SETG01",  "airline fares"),
    "motor_veh_ins_sa":    ("CUSR0000SETE",    "motor vehicle insurance"),
    # medical
    "medical_sa":          ("CUSR0000SAM",     "medical care"),
    "medical_comm_sa":     ("CUSR0000SAM1",    "medical care commodities"),
    "medical_svcs_sa":     ("CUSR0000SAM2",    "medical care services"),
    "physicians_sa":       ("CUSR0000SEMC01",  "physicians' services"),
    "hospital_sa":         ("CUSR0000SEMD01",  "hospital services"),
    "prescription_sa":     ("CUSR0000SEMF01",  "prescription drugs"),
    # recreation / education & communication / other
    "recreation_sa":       ("CUSR0000SAR",     "recreation"),
    "educ_comm_sa":        ("CUSR0000SAE",     "education and communication"),
    "education_sa":        ("CUSR0000SAE1",    "education"),
    "communication_sa":    ("CUSR0000SAE2",    "communication"),
    "other_goods_sa":      ("CUSR0000SAG",     "other goods and services"),
    "personal_care_sa":    ("CUSR0000SAG1",    "personal care"),
    "tobacco_sa":          ("CUSR0000SEGA",    "tobacco and smoking products"),

    # Never-revised NSA twin for each topic's anchor index. BLS re-estimates seasonal
    # factors every February and revises the previous five years of *seasonally
    # adjusted* data, so a 2005 release's SA figures no longer match today's SA series
    # (measured: only ~35% of stated 1-month SA changes reproduce). The NSA indexes are
    # final when published, so these are the channels a stated number can be checked
    # against exactly. Appended at the end on purpose: `load_history` batches series in
    # dict order, so inserting earlier would invalidate the cached API responses.
    "food_nsa":            ("CUUR0000SAF1",    "food"),
    "food_at_home_nsa":    ("CUUR0000SAF11",   "food at home"),
    "food_away_nsa":       ("CUUR0000SEFV",    "food away from home"),
    "energy_nsa":          ("CUUR0000SA0E",    "energy"),
    "gasoline_nsa":        ("CUUR0000SETB01",  "gasoline (all types)"),
    "core_nsa":            ("CUUR0000SA0L1E",  "all items less food and energy"),
    "shelter_nsa":         ("CUUR0000SAH1",    "shelter"),
    "housing_nsa":         ("CUUR0000SAH",     "housing"),
    "transportation_nsa":  ("CUUR0000SAT",     "transportation"),
    "apparel_nsa":         ("CUUR0000SAA",     "apparel"),
    "medical_nsa":         ("CUUR0000SAM",     "medical care"),
    "recreation_nsa":      ("CUUR0000SAR",     "recreation"),
    "educ_comm_nsa":       ("CUUR0000SAE",     "education and communication"),
    "other_goods_nsa":     ("CUUR0000SAG",     "other goods and services"),
}

ID_TO_KEY = {sid: k for k, (sid, _) in SERIES.items()}


def _chunk(xs: List, n: int) -> List[List]:
    return [xs[i:i + n] for i in range(0, len(xs), n)]


def load_history(cache_dir: Path, start_year: int, end_year: int,
                 delay_s: float = 1.0, allow_network: bool = True
                 ) -> Dict[str, Dict[str, float]]:
    """{channel_key: {"YYYY-MM": value}} for every series in SERIES.

    api.bls.gov v1 caps a request at 25 series and 10 years and an IP at 25
    requests/day, so this is 2 series-batches x 5 decade-chunks = 10 requests for
    1984-2026. Everything is cached under `cache_dir`, so reruns cost nothing.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    out: Dict[str, Dict[str, float]] = {k: {} for k in SERIES}
    ids = [sid for sid, _ in SERIES.values()]
    batches = _chunk(ids, 25)
    session = requests.Session()
    spent = 0

    for bi, batch in enumerate(batches):
        year = start_year
        while year <= end_year:
            chunk_end = min(year + 9, end_year)
            cf = cache_dir / f"hist_b{bi}_{year}_{chunk_end}.json"
            if cf.exists():
                data = json.loads(cf.read_text())
            else:
                if not allow_network:
                    raise SystemExit(f"missing cache {cf} and network disabled")
                if spent:
                    time.sleep(delay_s)
                resp = session.post(API_URL, json={"seriesid": batch,
                                                   "startyear": str(year),
                                                   "endyear": str(chunk_end)}, timeout=90)
                resp.raise_for_status()
                data = resp.json()
                spent += 1
                if data.get("status") != "REQUEST_SUCCEEDED":
                    raise SystemExit(f"BLS API refused batch {bi} {year}-{chunk_end}: "
                                     f"{data.get('status')} {data.get('message')}")
                cf.write_text(json.dumps(data))
                print(f"  fetched batch {bi} {year}-{chunk_end} "
                      f"(request {spent} this run)", file=sys.stderr)
            for s in data.get("Results", {}).get("series", []):
                key = ID_TO_KEY.get(s["seriesID"])
                if key is None:
                    continue
                for pt in s.get("data", []):
                    period = pt.get("period", "")
                    if not period.startswith("M") or period == "M13":
                        continue
                    try:
                        out[key][f"{int(pt['year']):04d}-{int(period[1:]):02d}"] = float(pt["value"])
                    except (ValueError, KeyError):
                        pass
            year = chunk_end + 1
    return out


def coverage(hist: Dict[str, Dict[str, float]]) -> List[Tuple[str, str, str, int]]:
    rows = []
    for k in SERIES:
        m = hist.get(k) or {}
        rows.append((k, min(m) if m else "-", max(m) if m else "-", len(m)))
    return rows


if __name__ == "__main__":
    ROOT = Path(__file__).resolve().parents[1]
    y0 = int(sys.argv[1]) if len(sys.argv) > 1 else 1984
    y1 = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
    h = load_history(ROOT / ".cache/api", y0, y1)
    print(f"{'channel':22} {'first':8} {'last':8} {'n':>5}")
    for k, a, b, n in coverage(h):
        print(f"{k:22} {a:8} {b:8} {n:5}")
