import abc
import asyncio
from typing import Any, Dict
from pydantic import BaseModel, Field

# --- Response Schemas ---


class TokenUsage(BaseModel):
    """Token usage tracking structure."""

    prompt_tokens: int = Field(default=0, description="Tokens used in the prompt")
    completion_tokens: int = Field(
        default=0, description="Tokens generated in the completion"
    )
    total_tokens: int = Field(default=0, description="Total tokens consumed")


class ProviderResponse(BaseModel):
    """Standardized LLM response validation model."""

    content: str = Field(..., description="The generated text response from the LLM")
    usage: TokenUsage = Field(
        default_factory=TokenUsage, description="Token usage statistics"
    )


# --- Rate Limiting Utility ---


class TokenBucket:
    def __init__(self, rate: float = 10.0, capacity: float = 10.0):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        import time

        self.last_refill = time.monotonic()

    def _refill(self):
        import time

        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed < 0:
            elapsed = 0
        self.tokens = min(self.capacity, max(0.0, self.tokens) + elapsed * self.rate)
        self.last_refill = now

    async def acquire(self):
        self._refill()
        while self.tokens < 1:
            await asyncio.sleep(0.01)
            self._refill()
        self.tokens -= 1


# Never wait longer than this on a Retry-After header. Tenacity does not catch
# exceptions raised by wait callables, so raising on a large value (the old
# behavior) crashed the whole pipeline on a real 429 with e.g. Retry-After: 30;
# the delay is capped instead.
MAX_RETRY_AFTER_DELAY = 10.0


class wait_retry_after:
    def __init__(self, fallback):
        self.fallback = fallback

    def __call__(self, retry_state):
        import httpx

        if retry_state.outcome.failed:
            ex = retry_state.outcome.exception()
            if isinstance(ex, httpx.HTTPStatusError) and ex.response.status_code == 429:
                retry_after = ex.response.headers.get("Retry-After")
                if retry_after:
                    delay = None
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        import email.utils
                        from datetime import datetime, timezone

                        try:
                            dt = email.utils.parsedate_to_datetime(retry_after)
                            delay = (dt - datetime.now(timezone.utc)).total_seconds()
                        except Exception:
                            delay = None
                    if delay is not None:
                        return min(max(delay, 0.0), MAX_RETRY_AFTER_DELAY)
        return self.fallback(retry_state)


# --- Base Provider ABC ---


class BaseProvider(abc.ABC):
    """
    Abstract Base Class for LLM providers.
    All concrete provider implementations (e.g. OpenAI, Anthropic, Grok) must inherit from this class.
    """

    def __init__(
        self,
        model_name: str,
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.extra_params = kwargs
        self.rate_limiter = TokenBucket(rate=10.0, capacity=10.0)
        # Concurrency-limit semaphores are created lazily, one per event loop
        # (see the ``semaphore`` property): an ``asyncio.Semaphore`` created in
        # ``__init__`` binds to whatever loop exists at construction time on
        # Python 3.10/3.11, so reusing the provider across ``asyncio.run()``
        # calls raised cross-event-loop errors or hung.
        self._semaphores: Dict[Any, asyncio.Semaphore] = {}

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """Per-event-loop concurrency semaphore (max 5 in-flight prompts)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        # Drop semaphores bound to closed loops so the map cannot grow
        # unboundedly across repeated asyncio.run() calls.
        for stale in [
            existing
            for existing in self._semaphores
            if existing is not None and existing.is_closed()
        ]:
            del self._semaphores[stale]
        sem = self._semaphores.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(5)
            self._semaphores[loop] = sem
        return sem

    @abc.abstractmethod
    async def send_prompt(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        """
        Sends a prompt to the provider's model asynchronously.

        Args:
            prompt: The string prompt to send to the model.
            **kwargs: Extra parameters to forward to the API (e.g., temperature, max_tokens).

        Returns:
            A standard dictionary matching the structure:
            {
                "content": str,
                "usage": {
                    "prompt_tokens": int,
                    "completion_tokens": int,
                    "total_tokens": int
                }
            }
        """
