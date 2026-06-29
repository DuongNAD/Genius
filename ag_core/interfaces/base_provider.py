import abc
import asyncio
from typing import Any, Dict
from pydantic import BaseModel, Field

# --- Response Schemas ---

class TokenUsage(BaseModel):
    """Token usage tracking structure."""
    prompt_tokens: int = Field(default=0, description="Tokens used in the prompt")
    completion_tokens: int = Field(default=0, description="Tokens generated in the completion")
    total_tokens: int = Field(default=0, description="Total tokens consumed")

class ProviderResponse(BaseModel):
    """Standardized LLM response validation model."""
    content: str = Field(..., description="The generated text response from the LLM")
    usage: TokenUsage = Field(default_factory=TokenUsage, description="Token usage statistics")


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
                    try:
                        delay = float(retry_after)
                        if delay > 10.0:
                            raise ValueError(f"Retry-After delay too large: {delay}s")
                        return delay
                    except ValueError as e:
                        if "Retry-After delay too large" in str(e):
                            raise
                        import email.utils
                        from datetime import datetime, timezone
                        try:
                            dt = email.utils.parsedate_to_datetime(retry_after)
                            delay = (dt - datetime.now(timezone.utc)).total_seconds()
                            if delay < 0:
                                delay = 0.0
                            if delay > 10.0:
                                raise ValueError(f"Retry-After delay too large: {delay}s")
                            return delay
                        except Exception:
                            pass
        return self.fallback(retry_state)


# --- Base Provider ABC ---

class BaseProvider(abc.ABC):
    """
    Abstract Base Class for LLM providers.
    All concrete provider implementations (e.g. OpenAI, Anthropic, Grok) must inherit from this class.
    """
    def __init__(self, model_name: str, api_key: str | None = None, base_url: str | None = None, **kwargs: Any) -> None:
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.extra_params = kwargs
        self.rate_limiter = TokenBucket(rate=10.0, capacity=10.0)
        self.semaphore = asyncio.Semaphore(5)

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
        pass
