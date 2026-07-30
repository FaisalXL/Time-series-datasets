#!/usr/bin/env python3
"""Bulk-download every distinct Stock SMART report file backing a candidate row.

Split out of the builder so the (slow, ~8 GB) network phase is restartable and can run
independently of extraction experiments. Files land in the same cache layout the builder
uses (`.cache/pdf/<sha1(url)[:20]>`), so a later build run finds them warm.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "pdf"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def cache_path(url: str) -> Path:
    return CACHE / hashlib.sha1(url.encode()).hexdigest()[:20]


def main() -> None:
    cands = json.loads(Path(sys.argv[1]).read_text())
    urls: list[str] = []
    seen = set()
    for r in cands:
        for u in r["urls"]:
            fid = re.search(r"fileId=(\d+)", u)
            key = fid.group(1) if fid else u
            if key not in seen:
                seen.add(key)
                urls.append(u)
    CACHE.mkdir(parents=True, exist_ok=True)
    todo = [u for u in urls if not cache_path(u).exists()]
    print(f"{len(urls)} distinct files, {len(todo)} to fetch", flush=True)
    ok = fail = 0
    t0 = time.time()
    for i, u in enumerate(todo, 1):
        for attempt in range(3):
            try:
                req = urllib.request.Request(u, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=180) as resp:
                    raw = resp.read()
                cache_path(u).write_bytes(raw)
                ok += 1
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    print(f"  ! FAIL {u} :: {e}", flush=True)
                    fail += 1
                else:
                    time.sleep(3.0 * (attempt + 1))
        time.sleep(0.25)
        if i % 100 == 0:
            print(f"  {i}/{len(todo)}  ok={ok} fail={fail}  {time.time()-t0:.0f}s", flush=True)
    print(f"done: ok={ok} fail={fail} in {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
