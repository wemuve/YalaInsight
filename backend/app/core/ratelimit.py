"""Rate limiting.

With ``YALA_REDIS_URL`` set, counters live in Redis (fixed one-minute windows) and are shared by every
app instance. Without it, an in-process sliding window is used — adequate only for a single instance.
If Redis becomes unreachable the limiter fails open (requests are allowed) and logs a warning, so an
infrastructure fault never locks every user out.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque

from ..config import settings

log = logging.getLogger("yala.ratelimit")


class MemoryRateLimiter:
    backend = "memory"

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_s: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window_s:
                q.popleft()
            if len(q) >= limit:
                return False, int(window_s - (now - q[0])) + 1
            q.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class RedisRateLimiter:
    backend = "redis"

    def __init__(self, url: str) -> None:
        import redis

        self._r = redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)

    def allow(self, key: str, limit: int, window_s: float = 60.0) -> tuple[bool, int]:
        bucket = int(time.time() // window_s)
        k = f"yala:rl:{key}:{bucket}"
        try:
            pipe = self._r.pipeline()
            pipe.incr(k)
            pipe.expire(k, int(window_s) + 1)
            count = pipe.execute()[0]
        except Exception as exc:  # noqa: BLE001 — fail open on infrastructure errors
            log.warning("rate limiter unavailable: %s", exc)
            return True, 0
        if count > limit:
            return False, int(window_s - time.time() % window_s) + 1
        return True, 0

    def ping(self) -> bool:
        try:
            return bool(self._r.ping())
        except Exception:  # noqa: BLE001
            return False

    def reset(self) -> None:
        for k in self._r.scan_iter("yala:rl:*"):
            self._r.delete(k)


RateLimiter = MemoryRateLimiter  # backwards-compatible name used in tests
limiter: MemoryRateLimiter | RedisRateLimiter = RedisRateLimiter(settings.redis_url) if settings.redis_url else MemoryRateLimiter()
