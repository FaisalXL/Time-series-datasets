#!/usr/bin/env python3
"""genesis_fetch.py -- paced GENESIS-Online fetcher for the CPI series this package needs.

The API contract here was established by testing, not by reading the docs; see the "GENESIS
web-service contract" section of README.md. The parts that shape this file:

  * form-encoded bodies only (a JSON body returns 415)
  * credentials for `data/*` go in HTTP HEADERS (in the body they 401 with Code 15)
  * at most 3 parallel requests; exceeding it returns HTTP 404 with Code 6, which reads as
    "not found" and is not
  * a failed request keeps holding its slot until a 15-minute reaper clears it, so probing
    costs real time and this fetcher is deliberately SEQUENTIAL
  * `job=true` is unavailable with token auth, so each request must fit the synchronous window

Credentials come from `.cache/genesis_token` (a personal API token, mode 600, `.cache/` is
gitignored). If a username+password pair is ever supplied for job mode, put it in
`.cache/genesis_login` as `username:password` -- and never in config.

Usage: genesis_fetch.py [--tables 61111-0002,61111-0004] [--timeslices 480] [--probe]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://genesis.destatis.de/genesisWS/rest/2020/"
ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache"
UA = "CPT-dataset-research (research; flnu@usc.edu)"

# The tables each release names for itself, so nothing has to be guessed. 61111-* is the NATIONAL
# CPI, which is the series the prose is actually about; 61121-* is the harmonised HICP, which is
# what the Eurostat route wrongly substituted.
DEFAULT_TABLES = ["61111-0002", "61111-0004", "61111-0006", "61121-0002", "61121-0006"]


def token() -> str:
    p = CACHE / "genesis_token"
    if not p.exists():
        raise SystemExit(f"missing {p} -- paste the 32-char API token from the GENESIS "
                         f'"Webservice (API)" modal into it (chmod 600)')
    return p.read_text().strip()


def post(path: str, timeout: int = 330, **params):
    """POST with credentials in the header. Returns (status, body_bytes, elapsed)."""
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "username": token(), "User-Agent": UA}
    req = urllib.request.Request(BASE + path,
                                 data=urllib.parse.urlencode(params).encode(),
                                 headers=headers, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read(), time.time() - t0
    except Exception as e:                                        # noqa: BLE001
        return type(e).__name__, str(e).encode(), time.time() - t0


def wait_for_slot(max_minutes: int = 35) -> bool:
    """Block until fewer than 3 requests are outstanding.

    `logincheck` doubles as the reaper: it terminates requests running over 15 minutes. Its 200
    response still carries the "exceeds the permitted limit" text while slots are busy, so the
    BODY is what has to be read, not the status.
    """
    for i in range(max_minutes):
        status, body, _ = post("helloworld/logincheck", timeout=90, language="en")
        if status == 200 and b"exceeds the permitted limit" not in body:
            return True
        print(f"  [{i:02d}] slots busy, waiting", flush=True)
        time.sleep(70)
    return False


def healthy() -> bool:
    """Is the data service answering at all?

    Measured 2026-08-20: `helloworld`, `catalogue` and `metadata` all answered 200 in seconds
    while EVERY `data/*` call -- three different tables, two endpoints, minimal parameters, from
    a clear slot state -- hung ~300 s and returned an HTML "Fatal Error" page rather than GENESIS
    JSON. An HTML crash page is a backend fault, not an authorisation refusal (those come back as
    clean JSON, e.g. Code 15). So this probe distinguishes "service unwell" from "my request is
    wrong" before a long run is attempted.
    """
    status, body, elapsed = post("data/table", timeout=330,
                                 name="11111-0001", area="all", language="en")
    head = body[:80].decode("utf-8", "replace").lstrip()
    ok = status == 200 and head.startswith("{") and '"Code":6' not in head
    print(f"  health probe (11111-0001): {status} in {elapsed:.0f}s -> "
          f"{'OK' if ok else 'DATA SERVICE NOT ANSWERING'}", flush=True)
    if not ok:
        print(f"    {head[:120]}", flush=True)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", default=",".join(DEFAULT_TABLES))
    ap.add_argument("--timeslices", default="480")
    ap.add_argument("--probe", action="store_true", help="health probe only")
    args = ap.parse_args()

    CACHE.mkdir(exist_ok=True)
    if not wait_for_slot():
        print("slots never cleared"); return 2
    if not healthy():
        print("\nThe data service is not answering. Nothing about this package's plan is wrong --\n"
              "re-run this script when it recovers. Everything else is verified: the token works,\n"
              "all five table codes resolve via metadata/table, and 61111-* is the national CPI\n"
              "the release prose is about.")
        return 3

    for name in args.tables.split(","):
        if not wait_for_slot():
            print("slots never cleared"); return 2
        status, body, elapsed = post("data/table", timeout=330, name=name, area="all",
                                     language="en", timeslices=args.timeslices)
        head = body[:80].decode("utf-8", "replace").lstrip()
        if status == 200 and head.startswith("{") and '"Code":6' not in head:
            out = CACHE / f"{name}.json"
            out.write_bytes(body)
            print(f"  {name}: {len(body):,} bytes in {elapsed:.0f}s -> {out.name}", flush=True)
        else:
            print(f"  {name}: FAILED {status} in {elapsed:.0f}s | {head[:110]}", flush=True)
        time.sleep(10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
