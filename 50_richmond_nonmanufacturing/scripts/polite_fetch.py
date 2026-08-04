"""Adaptive, connection-reusing Wayback fetcher.

Measured behaviour that motivates this module (2026-07-30, live):

  * A burst of 16 concurrent `web.archive.org` fetches gets the client throttled at the TCP
    level -- `[Errno 111] Connection refused` -- within a couple of seconds, and once tripped it
    refuses even a single sequential request. It clears again within seconds of going quiet.
  * `state_sources._http_get` retried *transport* errors but returned immediately on any
    `HTTPError`, and its retry budget (3, fixed backoff) is far too small for a throttle that
    rejects every in-flight request at once. Under a 16-worker fan-out this turned into
    **81 of 88 Iowa PDFs recorded as permanent failures** -- indistinguishable, downstream, from
    "the archive doesn't have these", which is precisely how a real archive silently looks empty.

So the throttle is a *rate* limit, not a concurrency limit, and it is recoverable. This module
enforces a single global request rate across all worker threads (a token bucket) and adapts it
AIMD-style: back off hard the moment the server pushes back, then creep the rate back up while
things are healthy. That keeps one shared notion of "how fast are we allowed to go" no matter how
many threads are running, which per-request retry loops cannot do.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# Status codes that mean "slow down / try again", as opposed to a real "this isn't here".
RETRY_STATUS = {429, 500, 502, 503, 504, 520, 522, 524}


class AdaptiveRate:
    """Global token bucket whose rate moves multiplicatively down / additively up.

    One instance is shared by every worker thread, so the *aggregate* request rate is what is
    controlled. Per-thread backoff cannot achieve this: with N threads, N independent backoffs
    still burst N-wide the instant they wake up, which is what tripped the throttle above.
    """

    def __init__(self, rate: float = 4.0, min_rate: float = 0.5, max_rate: float = 6.0) -> None:
        self.lock = threading.Lock()
        self.rate = rate
        self.min_rate, self.max_rate = min_rate, max_rate
        self.next_slot = time.monotonic()
        self.ok_streak = 0
        self.penalty_until = 0.0
        # Backoffs *in a row* (decays on success) -- drives the quiet period. Distinct from
        # `n_backoff`, which is a lifetime counter kept only for reporting.
        self.consec_backoff = 0
        # observability
        self.n_ok = self.n_retry = self.n_fail = 0
        self.n_backoff = 0

    def acquire(self) -> None:
        """Block until this thread may issue one request."""
        while True:
            with self.lock:
                now = time.monotonic()
                wait = max(self.next_slot - now, self.penalty_until - now)
                if wait <= 0:
                    self.next_slot = now + 1.0 / self.rate
                    return
            time.sleep(min(wait, 5.0))

    def report_ok(self) -> None:
        with self.lock:
            self.n_ok += 1
            self.ok_streak += 1
            # Additive increase, but only after a healthy run, so we probe upward gently.
            if self.ok_streak >= 25:
                self.rate = min(self.max_rate, self.rate + 0.5)
                self.ok_streak = 0
                # Decay the backoff count too. Without this it only ever grows, so the quiet
                # period below saturates at its 30s cap for the rest of the run and throughput
                # never recovers -- measured: a run cruising at 4-5/s fell to ~1.9/s and stayed
                # there after one bad patch, which would have turned a ~2h harvest into ~5.5h.
                # The penalty should track *current* conditions, not lifetime history.
                self.consec_backoff = max(0, self.consec_backoff - 1)

    def report_throttled(self) -> None:
        """Multiplicative decrease + a global quiet period.

        The quiet period is the important half: the throttle rejects *everything* in flight, so
        the only useful response is for the whole pool to go silent briefly, not for each thread
        to retry on its own schedule.
        """
        with self.lock:
            self.n_backoff += 1
            self.consec_backoff = min(self.consec_backoff + 1, 8)
            self.ok_streak = 0
            self.rate = max(self.min_rate, self.rate * 0.6)
            quiet = min(20.0, 1.2 * (1.6 ** self.consec_backoff)) * (0.7 + 0.6 * random.random())
            self.penalty_until = max(self.penalty_until, time.monotonic() + quiet)

    def snapshot(self) -> dict:
        with self.lock:
            return {"rate": round(self.rate, 2), "ok": self.n_ok, "retries": self.n_retry,
                    "failed": self.n_fail, "backoffs": self.n_backoff}


def make_session(pool: int = 32) -> requests.Session:
    """Keep-alive session. Connection reuse matters here beyond latency: the throttle is triggered
    partly by new-connection churn, and reusing sockets cuts the handshake rate substantially."""
    s = requests.Session()
    s.headers.update(UA)
    ad = HTTPAdapter(pool_connections=pool, pool_maxsize=pool, max_retries=0)
    s.mount("https://", ad)
    s.mount("http://", ad)
    return s


def get(session: requests.Session, url: str, rate: AdaptiveRate, *,
        attempts: int = 8, timeout: int = 45) -> tuple[Optional[bytes], str]:
    """Fetch one URL under the shared rate limit.

    Returns `(body, status)` where status is one of `ok` / `http_404` / `http_<code>` /
    `throttled_giveup` / `error_<Exception>`. Distinguishing a real 404 from an exhausted
    retry budget is the point: only the former means "this document does not exist".
    """
    last = "unknown"
    for attempt in range(attempts):
        rate.acquire()
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200:
                rate.report_ok()
                return resp.content, "ok"
            if resp.status_code in RETRY_STATUS:
                last = f"http_{resp.status_code}"
                with rate.lock:
                    rate.n_retry += 1
                rate.report_throttled()
                continue
            # 404/403/etc: a real answer, not congestion. Don't burn the budget.
            rate.report_ok()
            return None, f"http_{resp.status_code}"
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last = f"error_{type(e).__name__}"
            with rate.lock:
                rate.n_retry += 1
            rate.report_throttled()
        except Exception as e:  # noqa: BLE001 - record and move on
            last = f"error_{type(e).__name__}"
            with rate.lock:
                rate.n_retry += 1
            time.sleep(1.0)
    with rate.lock:
        rate.n_fail += 1
    return None, f"giveup_{last}"


def wayback_url(timestamp: str, original_url: str) -> str:
    """`id_` = return the archived bytes verbatim, with no Wayback toolbar injection."""
    return f"https://web.archive.org/web/{timestamp}id_/{original_url}"
