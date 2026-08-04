#!/usr/bin/env python3
"""Harvest every C3S Climate Bulletin page and every data CSV it links, to `.cache/`.

Split from the build for the same reason as the Fed-survey packages: caching raw bytes is
what lets later design questions be settled by measurement rather than argument, and the
extractor can be rewritten without re-fetching.

Two rules this enforces:
  * **A fetch failure is never recorded as "the bulletin does not exist."** Statuses are kept
    verbatim (`ok` / `http_404` / `giveup_*`) and `--report` counts them separately; only
    `http_404` on *every* slug means the month is absent.
  * **The universe is tried, not assumed** -- every slug for every month in the calendar, so
    the era boundaries fall out of the data (see `c3ssrc`).

Usage:
  python scripts/harvest.py --pages       # bulletin HTML for all themes/months
  python scripts/harvest.py --csvs        # every CSV linked from the cached pages
  python scripts/harvest.py               # both
  python scripts/harvest.py --report      # coverage of what is cached
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c3ssrc                                              # noqa: E402
import polite_fetch as pf                                  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"

UA = {"User-Agent": "CPT-dataset-research flnu@usc.edu"}


def paths(cfg: dict):
    html = ROOT / cfg["data"]["html_cache_dir"]
    csv = ROOT / cfg["data"]["csv_cache_dir"]
    html.mkdir(parents=True, exist_ok=True)
    csv.mkdir(parents=True, exist_ok=True)
    return html, csv


def ledger_path(cfg: dict) -> Path:
    return ROOT / cfg["data"]["html_cache_dir"] / ".." / "harvest_ledger.json"


def load_ledger(cfg) -> Dict[str, str]:
    p = ledger_path(cfg).resolve()
    return json.loads(p.read_text()) if p.exists() else {}


def save_ledger(cfg, led: Dict[str, str]) -> None:
    ledger_path(cfg).resolve().write_text(json.dumps(led, indent=1, sort_keys=True))


def last_month() -> str:
    t = dt.date.today().replace(day=1) - dt.timedelta(days=1)
    return f"{t.year:04d}-{t.month:02d}"


def is_bulletin(body: bytes) -> bool:
    """Does this look like a real bulletin page rather than the site's 404 shell?

    The C3S 404 page returns HTTP 200 in some paths and is ~155 kB of chrome, so the status
    code alone cannot be trusted; a real bulletin always links its own figure data or names
    the bulletin navigation.
    """
    s = body.decode("utf8", "ignore")
    if "Page not found" in s or "page-not-found" in s:
        return False
    return ("bulletin-navigation" in s) or bool(re.search(r'href="[^"]+\.csv', s))


# --- pages -----------------------------------------------------------------

def harvest_pages(cfg: dict, session, rate, retry_absent: bool = False) -> dict:
    html_dir, _ = paths(cfg)
    led = load_ledger(cfg)
    first = cfg["data"].get("first_month") or c3ssrc.FIRST_MONTH
    last = cfg["data"].get("end_month") or last_month()
    themes = cfg["data"]["themes"]
    stat = collections.Counter()
    slug_era: Dict[str, Dict[str, str]] = collections.defaultdict(dict)

    for ym in c3ssrc.months(first, last):
        for theme in themes:
            dest = html_dir / c3ssrc.local_name(theme, ym)
            key = dest.name
            if dest.exists() and dest.stat().st_size > 0:
                stat["cached"] += 1
                if led.get(key, "").startswith("ok:"):
                    slug_era[theme][ym] = led[key].split("::")[-1]
                continue
            if not retry_absent and led.get(key) == "absent_all_slugs":
                stat["known_absent"] += 1
                continue
            got = False
            statuses = []
            for cand in c3ssrc.candidates(theme, ym):
                body, status = pf.get(session, cand.url, rate, attempts=6, timeout=45)
                statuses.append(f"{cand.slug}:{status}")
                if status == "ok" and body and is_bulletin(body):
                    dest.write_bytes(body)
                    led[key] = f"ok:{cand.url}::{cand.slug}"
                    slug_era[theme][ym] = cand.slug
                    stat["fetched"] += 1
                    got = True
                    break
                if status == "ok":
                    stat["page_was_404_shell"] += 1
            if not got:
                # every slug answered; distinguish "not published" from "we failed"
                hard = all(s.split(":", 1)[1].startswith("http_4") or "404" in s for s in statuses)
                led[key] = "absent_all_slugs" if hard else "unresolved:" + ",".join(statuses)
                stat["absent" if hard else "unresolved"] += 1
            if stat["fetched"] % 20 == 0 and stat["fetched"]:
                save_ledger(cfg, led)
                print(f"  pages {dict(stat)} rate={rate.snapshot()}", file=sys.stderr)
    save_ledger(cfg, led)
    print(f"pages done: {dict(stat)}", file=sys.stderr)
    return {"stat": dict(stat),
            "slug_eras": {t: era_spans(v) for t, v in slug_era.items()}}


def era_spans(ym_to_slug: Dict[str, str]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = collections.defaultdict(list)
    for ym, slug in sorted(ym_to_slug.items()):
        out[slug].append(ym)
    return {s: [v[0], v[-1], str(len(v))] for s, v in out.items()}


# --- CSVs ------------------------------------------------------------------

_CSV_HREF = re.compile(r'href="([^"]+?\.csv[^"]*)"')


def csv_urls_from(body: bytes) -> List[str]:
    out, seen = [], set()
    for h in _CSV_HREF.findall(body.decode("utf8", "ignore")):
        u = h if h.startswith("http") else c3ssrc.BASE + h
        u = u.split("?")[0]
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def csv_local(url: str) -> str:
    """Cache name for a CSV. The path matters, not just the basename: C3S serves several
    distinct files under one basename from different month folders (and the same basename
    with `_0`/`v01.1` suffixes), so a basename-only cache silently collides."""
    tail = url.split("/sites/default/files/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9._-]", "_", tail)[-180:]


def harvest_csvs(cfg: dict, session, rate, workers: int = 4) -> dict:
    """Fetch every figure CSV the cached pages link.

    A small thread pool shares one rate limiter. The limiter controls the *aggregate*
    request rate, which is the whole reason it exists, so concurrency and politeness are not
    in tension here. Sequentially this pass wedged: ~1,100 small static files one at a time
    spent its life inside the limiter's quiet periods while the server was answering in 0.5s,
    and 40 minutes produced 9 files.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    html_dir, csv_dir = paths(cfg)
    led = load_ledger(cfg)
    stat = collections.Counter()
    index: Dict[str, List[str]] = {}
    todo: Dict[str, str] = {}
    for page in sorted(html_dir.glob("*.html")):
        urls = csv_urls_from(page.read_bytes())
        index[page.name] = [csv_local(u) for u in urls]
        for u in urls:
            name = csv_local(u)
            dest = csv_dir / name
            if dest.exists() and dest.stat().st_size > 0:
                stat["cached"] += 1
            elif led.get("csv::" + name, "").startswith("http_4"):
                stat["known_absent"] += 1
            else:
                # Anything not cached and not a hard 404 is retried, including a previous
                # run's `giveup_*`: a connection error is a fact about the network, never
                # about whether C3S published the file.
                todo[name] = u
    # written up front: the index is a pure function of the pages, so the builder must not
    # have to wait for the fetch pass to finish before it can resolve a section's series
    (csv_dir / ".." / "csv_index.json").resolve().write_text(json.dumps(index, indent=1))
    print(f"  csvs: {len(todo)} to fetch, {stat['cached']} already cached", file=sys.stderr)

    lock = threading.Lock()

    def one(item):
        name, u = item
        body, status = pf.get(session, u, rate, attempts=5, timeout=60)
        ok = (status == "ok" and body
              and not body.lstrip()[:200].lower().startswith(b"<!doctype"))
        with lock:
            if ok:
                (csv_dir / name).write_bytes(body)
                led["csv::" + name] = "ok:" + u
                stat["fetched"] += 1
            else:
                # C3S answers a missing CSV with its HTML 404 shell at status 200
                led["csv::" + name] = status if status != "ok" else "html_shell"
                stat[status if status != "ok" else "html_shell"] += 1
            done = stat["fetched"] + stat["html_shell"]
            if done and done % 100 == 0:
                save_ledger(cfg, led)
                print(f"  csvs {dict(stat)} rate={rate.snapshot()}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, sorted(todo.items())))
    save_ledger(cfg, led)
    (csv_dir / ".." / "csv_index.json").resolve().write_text(json.dumps(index, indent=1))
    print(f"csvs done: {dict(stat)}", file=sys.stderr)
    return {"stat": dict(stat), "pages_indexed": len(index)}


# --- report ----------------------------------------------------------------

def report(cfg: dict) -> dict:
    html_dir, csv_dir = paths(cfg)
    led = load_ledger(cfg)
    pages = collections.defaultdict(list)
    for p in sorted(html_dir.glob("*.html")):
        theme, ymc = p.stem.rsplit("_", 1)
        pages[theme].append(f"{ymc[:4]}-{ymc[4:]}")
    out = {"themes": {}}
    for theme, yms in sorted(pages.items()):
        yms.sort()
        out["themes"][theme] = {"months": len(yms), "span": [yms[0], yms[-1]],
                               "per_year": dict(sorted(collections.Counter(y[:4] for y in yms).items()))}
    slugs = collections.Counter(v.split("::")[-1] for k, v in led.items()
                                if k.endswith(".html") and v.startswith("ok:"))
    out["slug_usage"] = dict(slugs)
    out["absent_months"] = sorted(k for k, v in led.items() if v == "absent_all_slugs")
    out["unresolved"] = sorted(k for k, v in led.items() if v.startswith("unresolved"))
    out["csv_files_cached"] = len(list(csv_dir.glob("*")))
    out["csv_missing_at_source"] = sum(1 for k, v in led.items()
                                       if k.startswith("csv::") and v in ("html_shell",))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest C3S Climate Bulletin pages + CSVs")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--pages", action="store_true")
    ap.add_argument("--csvs", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--workers", type=int, default=4,
                    help="concurrent CSV fetches, sharing one rate limiter")
    ap.add_argument("--retry-absent", action="store_true",
                    help="re-attempt months previously recorded absent (use after a throttle)")
    args = ap.parse_args()
    cfg = yaml.safe_load(args.config.read_text())
    if args.report:
        print(json.dumps(report(cfg), indent=2))
        return
    session = pf.make_session()
    session.headers.update(UA)
    rate = pf.AdaptiveRate(rate=4.0, min_rate=0.5, max_rate=8.0)
    out = {}
    if args.pages or not args.csvs:
        out["pages"] = harvest_pages(cfg, session, rate, retry_absent=args.retry_absent)
    if args.csvs or not args.pages:
        out["csvs"] = harvest_csvs(cfg, session, rate, workers=args.workers)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
