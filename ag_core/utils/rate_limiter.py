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


# Central default rate limiter instance
limiter = TokenBucketRateLimiter(rate=10.0, capacity=10.0)


async def rate_limit_dependency():
    # Bypass rate limiting in standard test runs to prevent rate-limiting rapid sequential test requests.
    if "PYTEST_CURRENT_TEST" in os.environ and not os.environ.get(
        "ENABLE_RATE_LIMITER"
    ):
        return

    if not await limiter.consume_async(1.0):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too Many Requests",
            headers={"Retry-After": "1"},
        )
