#!/usr/bin/env python3
"""Stage 1 of the full build: discover and cache every archived NASS state Crop Progress report.

Why this is a separate stage. The original build did discovery -> fetch -> PDF-extract -> pair ->
emit in one pass, so every design iteration (window shape, text floors, channel sets, narrative
cleaning) re-paid the whole Wayback cost. At full scope that cost is ~39k PDF fetches, which is
the thing that made this a "server-side run, budget like 25_noaa_nwps_flood". Splitting it means
the network is touched exactly once and everything downstream is a local, seconds-to-minutes loop.

Two sub-stages, both resumable and both safe to re-run (already-cached work is skipped):

  discover : per state, one whole-folder Wayback CDX query per hub-folder variant ->
             .cache/candidates/{ALPHA}.json  (list of [timestamp, url] + an `ok` flag so a
             transient Wayback failure is never mistaken for an empty archive)
  fetch    : every candidate PDF -> .cache/pdf/{ALPHA}/{sha1}.pdf, indexed in index.jsonl

Raw bytes are cached (not extracted text) on purpose: the column-gutter detector and the
narrative cleaner are exactly the parts most likely to need another pass, and re-extracting from
local disk is minutes instead of hours.

Usage:
    python scripts/harvest_text.py discover [--states IA,KS] [--workers 12]
    python scripts/harvest_text.py fetch    [--states IA,KS] [--workers 16]
    python scripts/harvest_text.py status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import polite_fetch as pf  # noqa: E402
import state_sources as ss  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent
CACHE = PKG_ROOT / ".cache"
CAND_DIR = CACHE / "candidates"
PDF_DIR = CACHE / "pdf"

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- discover


def cdx_list_all_pdfs(session, prefix: str, rate) -> Optional[list[tuple[str, str]]]:
    """Whole-folder CDX listing, via the shared rate limiter.

    `state_sources.cdx_list_all_pdfs` used the weak retry path (`_http_get` gives up immediately
    on any HTTPError, so a 429 reads as "folder is empty"). Under even 4-way parallelism that
    produced `AR: 0 candidates` against a scouted 794 -- the same symptom the 2026-07-29 rollout
    wrote off as "transient Wayback query failures" for 9 states. Returns None on a *failed
    query*, [] only on a genuinely empty folder.
    """
    q = (f"{ss.CDX_API}?url={prefix}/&matchType=prefix&output=json"
         f"&filter=statuscode:200&collapse=urlkey&limit=20000")
    raw, status = pf.get(session, q, rate, attempts=10, timeout=90)
    if raw is None:
        return None if status.startswith("giveup") else []
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    return [(row[1], row[2]) for row in (data[1:] if data else []) if row[2].lower().endswith(".pdf")]


def discover_state(alpha: str, cfg, session, rate) -> dict:
    """Whole-folder CDX listing for one state, across every hub-folder spelling variant.

    `ok` requires that EVERY prefix query got a real answer. The old `any_ok` rule marked a state
    complete when a single empty folder-variant answered while the variant holding the real
    archive had errored -- so a throttled query became a permanent "this state has no data", with
    nothing downstream able to tell the difference.
    """
    seen: dict[str, str] = {}
    per_prefix = {}
    all_ok = True
    for prefix in cfg.base_prefixes:
        res = cdx_list_all_pdfs(session, prefix, rate)
        if res is None:
            per_prefix[prefix] = None  # query itself failed
            all_ok = False
            continue
        per_prefix[prefix] = len(res)
        for ts, url in res:
            seen.setdefault(url, ts)
    out = {
        "alpha": alpha,
        "ok": all_ok,
        "per_prefix": per_prefix,
        "candidates": sorted([[ts, url] for url, ts in seen.items()], key=lambda r: r[1]),
    }
    CAND_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CAND_DIR / f"{alpha}.json.tmp"
    with open(tmp, "w") as f:
        json.dump(out, f)
    tmp.replace(CAND_DIR / f"{alpha}.json")
    failed = [p for p, v in per_prefix.items() if v is None]
    log(f"  {alpha}: {len(out['candidates'])} candidates  ok={all_ok}"
        f"{f'  ({len(failed)} prefix queries failed)' if failed else ''}")
    return out


def cmd_discover(states: list[str], workers: int, force: bool) -> None:
    todo = []
    for a in states:
        p = CAND_DIR / f"{a}.json"
        if p.exists() and not force:
            try:
                prev = json.loads(p.read_text())
                # Re-run states whose previous discovery failed outright; keep real results.
                if prev.get("ok"):
                    continue
            except Exception:
                pass
        todo.append(a)
    log(f"discover: {len(todo)} states to query ({len(states) - len(todo)} already cached)")
    rate = pf.AdaptiveRate(rate=2.0, max_rate=6.0)
    session = pf.make_session(pool=max(8, workers * 2))
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(discover_state, a, ss.STATE_CONFIGS[a], session, rate): a
                    for a in todo}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    log(f"  {futs[fut]}: ERROR {e}")
    finally:
        session.close()
    log(f"discover done  {rate.snapshot()}")


# ---------------------------------------------------------------- fetch


def _sha(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


class Counter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.n = self.ok = self.bad = self.skip = 0
        self.t0 = time.time()

    def bump(self, kind: str, total: int, alpha: str, rate) -> None:
        with self.lock:
            self.n += 1
            setattr(self, kind, getattr(self, kind) + 1)
            n = self.n
            el = time.time() - self.t0
        if n % 200 == 0:
            log(f"  {alpha}: {n}/{total}  ok={self.ok} bad={self.bad}  "
                f"{n/max(el,1e-9):.2f}/s  {rate.snapshot()}")


def fetch_state(alpha: str, workers: int, rate: pf.AdaptiveRate) -> dict:
    cand_path = CAND_DIR / f"{alpha}.json"
    if not cand_path.exists():
        log(f"  {alpha}: no candidate file, skipping (run discover first)")
        return {}
    meta = json.loads(cand_path.read_text())
    cands = meta["candidates"]
    out_dir = PDF_DIR / alpha
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "index.jsonl"

    # Only *final* outcomes count as done. A `giveup_*` / `fetch_failed` row means our retry
    # budget ran out while the server was pushing back -- it says nothing about whether the
    # document exists, so re-running must retry it. Treating those as done is what would bake a
    # transient throttle into the corpus as missing data.
    FINAL = {"ok", "not_pdf", "http_404", "http_403"}
    have = set()
    if index_path.exists():
        with open(index_path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("status") in FINAL:
                    have.add(rec["url"])

    todo = [(ts, url) for ts, url in cands if url not in have]
    log(f"  {alpha}: {len(cands)} candidates, {len(todo)} to fetch, {len(have)} already cached")
    if not todo:
        return {"alpha": alpha, "fetched": 0, "cached": len(have)}

    ctr = Counter()
    write_lock = threading.Lock()
    fh = open(index_path, "a")
    session = pf.make_session(pool=max(8, workers * 2))

    def one(ts: str, url: str) -> None:
        raw, status = pf.get(session, pf.wayback_url(ts, url), rate)
        rec = {"url": url, "ts": ts, "status": status}
        if raw and raw.lstrip().startswith(b"%PDF"):
            h = _sha(url)
            (out_dir / f"{h}.pdf").write_bytes(raw)
            rec.update({"sha1": h, "bytes": len(raw), "status": "ok"})
            kind = "ok"
        else:
            # Keep the *reason* distinct. `giveup_*` means the retry budget ran out under
            # throttling and the URL is worth another pass later; http_404 / not_pdf are final.
            if raw:
                rec["status"] = "not_pdf"
            rec["bytes"] = len(raw) if raw else 0
            kind = "bad"
        with write_lock:
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
        ctr.bump(kind, len(todo), alpha, rate)

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(one, ts, url) for ts, url in todo]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    log(f"  {alpha}: fetch error {e}")
    finally:
        fh.close()
        session.close()
    log(f"  {alpha}: DONE ok={ctr.ok} bad={ctr.bad}  {rate.snapshot()}")
    return {"alpha": alpha, "fetched": ctr.ok, "bad": ctr.bad}


def cmd_fetch(states: list[str], workers: int, rate_start: float) -> None:
    t0 = time.time()
    # One rate limiter for the whole run: the throttle is global to our IP, so per-state or
    # per-thread limiters would each have to rediscover the ceiling and would burst on handoff.
    rate = pf.AdaptiveRate(rate=rate_start)
    for alpha in states:
        fetch_state(alpha, workers, rate)
    log(f"fetch complete in {(time.time()-t0)/60:.1f} min  {rate.snapshot()}")


# ---------------------------------------------------------------- status


def cmd_status(states: list[str]) -> None:
    tot_c = tot_p = 0
    rows = []
    for a in states:
        cp = CAND_DIR / f"{a}.json"
        nc, ok = 0, None
        if cp.exists():
            m = json.loads(cp.read_text())
            nc, ok = len(m["candidates"]), m["ok"]
        npdf = len(list((PDF_DIR / a).glob("*.pdf"))) if (PDF_DIR / a).exists() else 0
        rows.append((a, nc, npdf, ok))
        tot_c += nc
        tot_p += npdf
    rows.sort(key=lambda r: -r[1])
    print(f"{'st':>3} {'cands':>7} {'pdfs':>7}  ok")
    for a, nc, npdf, ok in rows:
        print(f"{a:>3} {nc:>7} {npdf:>7}  {ok}")
    print(f"{'ALL':>3} {tot_c:>7} {tot_p:>7}   ({len(rows)} states)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["discover", "fetch", "status"])
    ap.add_argument("--states", default=None, help="comma list; default = all in STATE_CONFIGS")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--rate", type=float, default=3.0, help="starting global req/s (adapts)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    # Harvest works on *pools*, not states: the six New England states share one regional
    # archive, so iterating states would fetch those 979 PDFs six times.
    if args.states:
        states = args.states.split(",")
    else:
        seen: dict[str, None] = {}
        for c in ss.STATE_CONFIGS.values():
            seen.setdefault(c.pool, None)
        states = list(seen)
    if args.cmd == "discover":
        cmd_discover(states, args.workers, args.force)
    elif args.cmd == "fetch":
        cmd_fetch(states, args.workers, args.rate)
    else:
        cmd_status(states)


if __name__ == "__main__":
    main()
