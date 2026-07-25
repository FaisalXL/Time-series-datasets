#!/usr/bin/env python3
"""Harvest USDM weekly narratives (XML) + regional drought-area series into .cache/.

Robots-legal: narratives come from /services/data/summary/xml/, which is outside
every Disallow prefix in droughtmonitor.unl.edu/robots.txt (/data/, /nadmdata/,
/webfiles/, /DmData/DataArchive.aspx). The retired builder used
/data/narrativepdf/ -- under the disallowed /data/ prefix.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
CACHE = PKG / ".cache"
XML_DIR = CACHE / "summary_xml"
API_DIR = CACHE / "api_rcc"

SITE = "https://droughtmonitor.unl.edu"
XML_URL = SITE + "/services/data/summary/xml/usdm_summary_{d}.xml"
SUMMARY_PAGE = SITE + "/Summary.aspx"
API = "https://usdmdataservices.unl.edu/api"

UA = "CPT-corpus-builder/1.0 (research corpus; contact flnu@usc.edu)"
DELAY = 0.7

# RegionalClimateCenterStatistics aoi codes -> the six official USDM regions,
# which are exactly the section headings the modern narrative uses.
REGIONS = {
    "1": "High Plains",
    "2": "Midwest",
    "3": "Northeast",
    "4": "South",
    "5": "Southeast",
    "6": "West",
}


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check_robots():
    """BOM-stripping robots check -- Python's robotparser is fooled by the BOM."""
    txt = fetch(SITE + "/robots.txt").decode("utf-8-sig", "replace")
    dis = []
    active = False
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            active = v == "*"
        elif k == "disallow" and active and v:
            dis.append(v)
    print(f"[robots] Disallow for *: {dis}")
    for path in ("/services/data/summary/xml/", "/data/narrativepdf/"):
        blocked = any(path.startswith(d) for d in dis)
        print(f"[robots] {path:34s} -> {'BLOCKED' if blocked else 'allowed'}")
    assert not any("/services/data/summary/xml/".startswith(d) for d in dis), \
        "narrative endpoint is robots-disallowed"
    return dis


def week_list():
    """Every USDM valid-date, scraped from the Summary.aspx archive dropdown."""
    cached = CACHE / "week_list.json"
    if cached.exists():
        return json.loads(cached.read_text())
    html = fetch(SUMMARY_PAGE).decode("utf-8", "replace")
    weeks = re.findall(r'<option[^>]*value="(\d{8})"', html)
    weeks = sorted(set(weeks))
    cached.write_text(json.dumps(weeks))
    print(f"[weeks] {len(weeks)}: {weeks[0]} .. {weeks[-1]}")
    return weeks


def harvest_narratives(weeks):
    XML_DIR.mkdir(parents=True, exist_ok=True)
    ok = miss = hit = 0
    for i, d in enumerate(weeks):
        dest = XML_DIR / f"{d}.xml"
        if dest.exists() and dest.stat().st_size > 0:
            hit += 1
            continue
        try:
            body = fetch(XML_URL.format(d=d))
            if len(body) < 200:
                raise ValueError(f"short body {len(body)}")
            dest.write_bytes(body)
            ok += 1
        except Exception as e:  # noqa: BLE001
            miss += 1
            print(f"[narr] MISS {d}: {e}")
        time.sleep(DELAY)
        if (i + 1) % 100 == 0:
            print(f"[narr] {i+1}/{len(weeks)}  new={ok} cached={hit} miss={miss}", flush=True)
    print(f"[narr] DONE new={ok} cached={hit} miss={miss}")


def harvest_series(start="2000-01-01", end="2026-12-31"):
    API_DIR.mkdir(parents=True, exist_ok=True)
    jobs = []
    for aoi in REGIONS:
        jobs.append((f"area_{aoi}", f"{API}/RegionalClimateCenterStatistics"
                     f"/GetDroughtSeverityStatisticsByAreaPercent"
                     f"?aoi={aoi}&startdate={start}&enddate={end}&statisticsType=1"))
        jobs.append((f"dsci_{aoi}", f"{API}/RegionalClimateCenterStatistics/GetDSCI"
                     f"?aoi={aoi}&startdate={start}&enddate={end}&statisticsType=1"))
    jobs.append(("area_us", f"{API}/USStatistics/GetDroughtSeverityStatisticsByAreaPercent"
                 f"?aoi=us&startdate={start}&enddate={end}&statisticsType=1"))
    jobs.append(("dsci_us", f"{API}/USStatistics/GetDSCI"
                 f"?aoi=us&startdate={start}&enddate={end}&statisticsType=1"))
    for name, url in jobs:
        dest = API_DIR / f"{name}.json"
        if dest.exists() and dest.stat().st_size > 0:
            print(f"[api] cached {name}")
            continue
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
        try:
            rows = json.loads(body)
        except Exception:  # noqa: BLE001
            print(f"[api] FAIL {name}: {body[:200]!r}")
            continue
        dest.write_bytes(body)
        print(f"[api] {name}: {len(rows)} rows")
        time.sleep(DELAY)


if __name__ == "__main__":
    CACHE.mkdir(parents=True, exist_ok=True)
    check_robots()
    harvest_series()
    weeks = week_list()
    print(f"[weeks] {len(weeks)} weeks {weeks[0]}..{weeks[-1]}")
    if "--series-only" not in sys.argv:
        harvest_narratives(weeks)
