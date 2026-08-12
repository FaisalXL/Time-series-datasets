"""Paced, cached ONS fetcher with a CROSS-PROCESS limiter.

ONS allows a burst of ~5 then hard-429s, and the penalty persists for ~30s. Two facts drive
this design:

  * The limiter must be GLOBAL, not per-process. A background crawler and a foreground dev
    script each pacing themselves politely still collide and both get throttled, so the pace
    is held in a lock-file shared by every process (one AIMD state, as on the Wayback work).
  * A throttle failure must NEVER be cached. A 429 body is 17 bytes; caching it would bake
    "the source has nothing here" into the corpus. Only 200s and real 404s are cached, and an
    exhausted retry chain returns 429 so the caller records UNKNOWN rather than empty.
"""
import fcntl, hashlib, os, struct, time, urllib.error, urllib.request
from pathlib import Path

CACHE = Path("/data/defu/.cache/ons61")
LOCK = CACHE / "_ratelimit.lock"
UA = {"User-Agent": "CPT-dataset-research flnu@usc.edu", "Accept": "*/*"}
MIN_GAP, MAX_GAP = 1.6, 20.0
_local = {"nlive": 0, "nhit": 0, "n429": 0, "n404": 0, "nfail": 0}


def _pace():
    """Claim the next slot under the shared gap; returns the gap in force."""
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a+b") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read(16)
            last, gap = struct.unpack("dd", raw) if len(raw) == 16 else (0.0, 2.0)
            gap = min(max(gap, MIN_GAP), MAX_GAP)
            now = time.time()
            wait = last + gap - now
            if wait > 0:
                time.sleep(wait)
                now = time.time()
            fh.seek(0); fh.truncate(); fh.write(struct.pack("dd", now, gap)); fh.flush()
            return gap
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _adjust(mult, floor_add=0.0):
    with open(LOCK, "a+b") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read(16)
            last, gap = struct.unpack("dd", raw) if len(raw) == 16 else (time.time(), 2.0)
            gap = min(max(gap * mult + floor_add, MIN_GAP), MAX_GAP)
            fh.seek(0); fh.truncate(); fh.write(struct.pack("dd", last, gap)); fh.flush()
            return gap
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _path(url):
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    slug = url.replace("https://www.ons.gov.uk/", "").replace("https://", "")
    slug = "".join(c if c.isalnum() or c in "-._" else "_" for c in slug)[:110]
    return CACHE / f"{slug}__{h}"


_index = None


def _build_index():
    """hash -> (path, is404), built with ONE readdir.

    The url hash is the identity; the slug prefix only makes the cache readable. Resolving by
    hash means changing the slugging scheme never silently re-downloads (this cache holds 23MB
    dataset CSVs) or, worse, re-enters the throttle. Indexed once rather than globbed per
    fetch, because at full scale that is 5,788 rescans of a 6,000-entry directory.
    """
    global _index
    _index = {}
    if not CACHE.exists():
        return
    for name in os.listdir(CACHE):
        if name.startswith("_") or name.endswith(".tmp"):
            continue
        is404 = name.endswith(".404")
        stem = name[:-4] if is404 else name
        if "__" in stem:
            _index[stem.rsplit("__", 1)[1]] = (CACHE / name, is404)


def _resolve(url):
    if _index is None:
        _build_index()
    return _index.get(_path(url).name.rsplit("__", 1)[1], (None, False))


def fetch(url, tries=6):
    """-> (status, bytes). 200 and 404 are cached; 429/None never are."""
    p = _path(url)
    hit, is404 = _resolve(url)
    if is404:
        return 404, b""
    if hit is not None:
        _local["nhit"] += 1
        return 200, hit.read_bytes()
    for attempt in range(tries):
        _pace()
        _local["nlive"] += 1
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                b = r.read()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp"); tmp.write_bytes(b); os.replace(tmp, p)
            if _index is not None:
                _index[p.name.rsplit("__", 1)[1]] = (p, False)
            # Decay must be fast enough to undo a backoff spike. At 0.98 per success it took ~90
            # clean fetches to walk back from one 429, so two concurrent fetchers ratcheted the
            # shared gap to 12.3s and stayed there -- the limiter was correct but unrecoverable.
            _adjust(0.90)
            return 200, b
        except urllib.error.HTTPError as e:
            try: e.read()
            except Exception: pass
            if e.code == 429:
                _local["n429"] += 1
                g = _adjust(1.5, 0.2)           # multiplicative backoff, shared
                time.sleep(g * (attempt + 1))
                continue
            if e.code in (404, 410):
                _local["n404"] += 1
                p.parent.mkdir(parents=True, exist_ok=True)
                q = p.with_suffix(".404"); q.write_bytes(b"")
                if _index is not None:
                    _index[p.name.rsplit("__", 1)[1]] = (q, True)
                return 404, b""
            _local["nfail"] += 1
            return e.code, b""
        except Exception:
            time.sleep(min(30.0, 2.0 * (attempt + 1)))
    _local["nfail"] += 1
    return 429, b""                             # UNKNOWN -- caller must not treat as empty


def stats(): return dict(_local)
