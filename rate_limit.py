"""In-process token-bucket rate limiter — no external dependencies."""
import threading
import time

from fastapi import HTTPException


class _TokenBucket:
    def __init__(self, rate: float, capacity: int):
        self._rate     = rate      # tokens refilled per second
        self._capacity = capacity  # max burst
        self._tokens   = float(capacity)
        self._last     = time.monotonic()
        self._lock     = threading.Lock()

    def consume(self, tokens: int = 1) -> bool:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self._capacity,
                self._tokens + (now - self._last) * self._rate,
            )
            self._last = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False


# 5 triggers/second, burst up to 10
_trigger_bucket = _TokenBucket(rate=5.0, capacity=10)


async def check_trigger_rate() -> None:
    if not _trigger_bucket.consume():
        raise HTTPException(429, "Timing trigger rate limit exceeded (max 5/s burst 10)")
