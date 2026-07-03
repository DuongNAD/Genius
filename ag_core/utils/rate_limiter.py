import os
import time
import threading
import asyncio
from fastapi import HTTPException, status


class TokenBucketRateLimiter:
    def __init__(self, rate: float = 10.0, capacity: float = 10.0):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_update = time.time()
        self.lock = threading.Lock()

    @property
    def async_lock(self) -> asyncio.Lock:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self.lock:
            if not hasattr(self, "_async_locks"):
                import weakref

                self._async_locks = weakref.WeakKeyDictionary()
            if loop is None:
                if not hasattr(self, "_none_lock"):
                    self._none_lock = asyncio.Lock()
                return self._none_lock
            if loop not in self._async_locks:
                self._async_locks[loop] = asyncio.Lock()
            return self._async_locks[loop]

    def reset(self):
        with self.lock:
            self.tokens = self.capacity
            self.last_update = time.time()

    def consume(self, tokens_to_consume: float = 1.0) -> bool:
        with self.lock:
            now = time.time()
            elapsed = now - self.last_update
            self.last_update = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            if self.tokens >= tokens_to_consume:
                self.tokens -= tokens_to_consume
                return True
            return False

    async def consume_async(self, tokens_to_consume: float = 1.0) -> bool:
        async with self.async_lock:
            with self.lock:
                now = time.time()
                elapsed = now - self.last_update
                self.last_update = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                if self.tokens >= tokens_to_consume:
                    self.tokens -= tokens_to_consume
                    return True
                return False


# Central default rate limiter instance (kept for backward compatibility and
# for the role-less case).
limiter = TokenBucketRateLimiter(rate=10.0, capacity=10.0)

# Per-role limiters. When serve.py hosts several skill servers in ONE process
# (the default menu / --auto-pilot), a single shared bucket is drained by normal
# pipeline fan-out — each role is polled concurrently per file — so legitimate
# traffic gets 429'd and one chatty stage starves the others. Give each role its
# own token bucket instead.
_role_limiters = {}
_role_limiters_lock = threading.Lock()


def get_role_limiter(
    role: str, rate: float = 10.0, capacity: float = 10.0
) -> TokenBucketRateLimiter:
    """Return (creating on first use) the token bucket dedicated to ``role``."""
    key = role or "_default"
    with _role_limiters_lock:
        rl = _role_limiters.get(key)
        if rl is None:
            rl = TokenBucketRateLimiter(rate=rate, capacity=capacity)
            _role_limiters[key] = rl
        return rl


def _rate_limiter_bypassed() -> bool:
    """Rate limiting is off in plain test runs (rapid sequential requests would
    otherwise 429) unless ENABLE_RATE_LIMITER is set."""
    return "PYTEST_CURRENT_TEST" in os.environ and not os.environ.get(
        "ENABLE_RATE_LIMITER"
    )


async def rate_limit_dependency():
    # Bypass rate limiting in standard test runs to prevent rate-limiting rapid sequential test requests.
    if _rate_limiter_bypassed():
        return

    if not await limiter.consume_async(1.0):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too Many Requests",
            headers={"Retry-After": "1"},
        )


def make_rate_limit_dependency(role: str):
    """FastAPI dependency that rate-limits using ``role``'s own token bucket, so
    skill servers co-hosted in one process don't share a single global budget."""
    rl = get_role_limiter(role)

    async def _role_rate_limit_dependency():
        if _rate_limiter_bypassed():
            return
        if not await rl.consume_async(1.0):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too Many Requests",
                headers={"Retry-After": "1"},
            )

    return _role_rate_limit_dependency
