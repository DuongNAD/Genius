import os
from typing import Any, Dict
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from ag_core.interfaces.base_provider import BaseProvider, ProviderResponse, TokenUsage

def _is_retriable(exception: BaseException) -> bool:
    if isinstance(exception, httpx.TimeoutException):
        return True
    if isinstance(exception, httpx.HTTPStatusError):
        # Do not retry on client authentication or validation errors (e.g. 400, 401, 403, 404)
        # But do retry on 429 (Rate Limit) and 5xx (Server Errors)
        return exception.response.status_code == 429 or exception.response.status_code >= 500
    return False

class AnthropicProvider(BaseProvider):
    """
    Anthropic Claude API provider implementation using raw async HTTP requests.
    """
    def __init__(self, model_name: str = "claude-3-5-sonnet-20241022", api_key: str | None = None, base_url: str | None = None, **kwargs: Any) -> None:
        api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        base_url = base_url or os.getenv("ANTHROPIC_BASE_URL") or "https://api.anthropic.com/v1"
        super().__init__(model_name=model_name, api_key=api_key, base_url=base_url, **kwargs)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retriable),
        reraise=True
    )
    async def _send_request_with_retry(self, client: httpx.AsyncClient, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Performs async POST request with Tenacity backoff retries."""
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        }
        url = f"{self.base_url.rstrip('/')}/messages"
        
        response = await client.post(url, json=payload, headers=headers, timeout=30.0)
        response.raise_for_status()
        return response.json()

    async def send_prompt(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        if not self.api_key:
            raise ValueError("Anthropic API key must be provided or set via ANTHROPIC_API_KEY environment variable.")
            
        # Extract max_tokens (default 1024, as Anthropic requires this key)
        extra = self.extra_params.copy()
        extra.update(kwargs)
        max_tokens = extra.pop("max_tokens", 1024)
        
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            **extra
        }
        
        async with httpx.AsyncClient() as client:
            res_json = await self._send_request_with_retry(client, payload)
            
        content = res_json["content"][0]["text"]
        usage_data = res_json.get("usage", {})
        
        prompt_tokens = usage_data.get("input_tokens", 0)
        completion_tokens = usage_data.get("output_tokens", 0)
        total_tokens = prompt_tokens + completion_tokens
        
        # Validate output shape
        response = ProviderResponse(
            content=content,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens
            )
        )
        return response.model_dump()
