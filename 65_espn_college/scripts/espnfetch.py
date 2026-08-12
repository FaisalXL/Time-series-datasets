#!/usr/bin/env python3
"""Paced, retrying, caching GET for ESPN's site API.

Split out of `build_cpt_jsonl.py` because the census walk needs the same fetch discipline as the
build, and because the build's original inline fetcher had two faults that a census would have
turned into fabricated numbers:

  1. **It gave up after 4 quick tries with a linear backoff.** ESPN answers a sustained walk with
     HTTP 502 rather than 429 — a first pass over college seasons drew 502s on roughly a fifth of
     calls. Those are throttle responses, not empty days, and a walk that reads them as "0 games"
     silently under-counts the universe. Backoff here is exponential with a shared inter-request
     gap that GROWS on throttle and decays on success (the AIMD limiter #61 needed for ONS).
  2. **A failed fetch must never be cached.** `cached_json` only writes real payloads. A cached
     throttle failure is permanent: it looks like a measurement forever after.

A 404 IS a real answer (a date with no games, an unsupported range form) and is returned as None
without being cached — cheap to re-ask, and caching it would freeze a wrong negative if the shape
of the endpoint changes.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

# Throttle responses ESPN actually emits. 502/503 are what a sustained walk draws; 429 is the
# documented one and appears rarely.
RETRY_CODES = {429, 500, 502, 503, 504}


class Fetcher:
    """One shared pace across every call made through this instance.

    `gap` is multiplicative-increase / gentle-decay: a throttle widens it immediately, successes
    walk it back toward `base_delay`. Without the increase a long walk keeps hammering at the rate
    that just failed; without the decay one early 502 slows the whole run to a crawl.
    """

    def __init__(self, cache_dir: Path, ua: str, timeout: int = 60, delay: float = 0.45,
                 max_gap: float = 12.0, tries: int = 7, verbose: bool = False):
        self.cache = Path(cache_dir)
        self.ua = ua
        self.timeout = int(timeout)
        self.base = float(delay)
        self.gap = float(delay)
        self.max_gap = float(max_gap)
        self.tries = int(tries)
        self.verbose = verbose
        self._last = 0.0
        # One lock guards the pace and the counters, so a thread pool sharing this instance is a
        # GLOBAL rate limit rather than N independent ones. Note what this does and does not buy:
        # the aggregate rate is 1/gap no matter how many workers there are -- workers only hide
        # per-request latency (~0.5s here), which alone is the difference between the ~1 req/s a
        # serial walk actually achieves at gap=0.45 and the 2.2 req/s that gap implies. Going
        # faster than that means lowering `gap`, not adding workers.
        self._lock = threading.RLock()
        self.stats = {"http": 0, "cache": 0, "retries": 0, "throttled": 0, "failed": 0,
                      "not_found": 0}

    # -- pacing -------------------------------------------------------------------------------
    def _wait(self) -> None:
        # The sleep is INSIDE the lock on purpose: it serialises scheduling (one request may start
        # per `gap`) while the request itself runs outside, so concurrency still yields throughput.
        with self._lock:
            now = time.time()
            w = self.gap - (now - self._last)
            if w > 0:
                time.sleep(w)
                now = time.time()
            self._last = now

    def _bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.stats[key] += n

    def _throttled(self) -> None:
        with self._lock:
            self.gap = min(self.gap * 1.6, self.max_gap)
            self.stats["throttled"] += 1

    def _ok(self) -> None:
        with self._lock:
            self.gap = max(self.base, self.gap * 0.90)

    # -- fetch --------------------------------------------------------------------------------
    def get(self, url: str) -> Optional[Any]:
        """Parsed JSON, or None for a genuine 404 / an exhausted retry budget.

        Callers that must distinguish "no games" from "we never got an answer" should check
        `stats['failed']` before and after, or use `get_strict`.
        """
        return self._get(url)[0]

    def get_strict(self, url: str):
        """-> (payload_or_None, ok). ok is False only when the retry budget ran out, i.e. the
        answer is unknown rather than empty. A census must not record unknown as zero."""
        return self._get(url)

    def _get(self, url: str):
        for attempt in range(self.tries):
            self._wait()
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": self.ua, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    payload = json.loads(r.read())
                self._bump("http")
                self._ok()
                return payload, True
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    self._bump("not_found")
                    return None, True          # a real answer: nothing here
                if e.code in RETRY_CODES:
                    self._throttled()
                else:
                    # 400/403 and friends: the URL is wrong, not the moment. Do not burn retries.
                    self._bump("failed")
                    if self.verbose:
                        print(f"    ! HTTP {e.code} {url}", flush=True)
                    return None, False
            except Exception:
                self._throttled()
            self._bump("retries")
            time.sleep(min(self.base * (2 ** attempt), 30.0))
            if self.verbose:
                print(f"    . retry {attempt + 1}/{self.tries} gap={self.gap:.2f}s", flush=True)
        self._bump("failed")
        if self.verbose:
            print(f"    ! gave up {url}", flush=True)
        return None, False

    def cached(self, url: str, rel: str):
        """Cache-backed `get`. `rel` is a path relative to the cache dir."""
        fp = self.cache / rel
        if fp.exists():
            try:
                self._bump("cache")
                return json.loads(fp.read_text()), True
            except Exception:
                pass                            # truncated cache file: refetch
        payload, ok = self._get(url)
        if payload is not None:                 # only real payloads are ever written
            fp.parent.mkdir(parents=True, exist_ok=True)
            tmp = fp.with_suffix(fp.suffix + ".part")
            tmp.write_text(json.dumps(payload))
            tmp.replace(fp)                     # atomic: no half-written cache entries
        return payload, ok


def params_tag(params: str) -> str:
    """Cache-key fragment naming the scoreboard params a payload was fetched under.

    Not cosmetic. The cache was originally keyed on the date alone, so when the `&limit=1000`
    truncation was found and removed, the corrected URL still read the TRUNCATED payload back out
    of the cache — a param fix that appears to change nothing is worse than the original bug.
    Counts fetched under different params now live in different directories.
    """
    if not params:
        return "bare"
    parts = [p for p in params.replace("&", " ").split() if p]
    return "-".join(p.replace("=", "") for p in parts) or "bare"


def scoreboard_rel(lg: dict, date: str) -> str:
    """Cache path for one league-day scoreboard, keyed on league AND params."""
    return f"census/scoreboard/{lg['league']}/{params_tag(lg.get('params', ''))}/{date}.json"


def completed_events(scoreboard: dict) -> list[dict]:
    """Events whose status says the game finished. ESPN sets `completed` on the status type; the
    name check catches payload shapes where only the name is populated."""
    out = []
    for ev in (scoreboard or {}).get("events") or []:
        st = ((ev.get("status") or {}).get("type") or {})
        if st.get("completed") or st.get("name") == "STATUS_FINAL":
            out.append(ev)
    return out


def flatten_plays(summary: dict) -> list[dict]:
    """Basketball exposes a flat `plays`; football nests plays under `drives.previous[]`."""
    plays = (summary or {}).get("plays") or []
    if plays:
        return plays
    out = []
    for drv in (((summary or {}).get("drives") or {}).get("previous")) or []:
        out.extend(drv.get("plays") or [])
    return out
