import os
from typing import Any, Dict
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from ag_core.interfaces.base_provider import BaseProvider, ProviderResponse, TokenUsage, wait_retry_after

def _is_retriable(exception: BaseException) -> bool:
    if isinstance(exception, httpx.TimeoutException):
        return True
    if isinstance(exception, httpx.HTTPStatusError):
        # Do not retry on client authentication or validation errors (e.g. 400, 401, 403, 404)
        # But do retry on 429 (Rate Limit) and 5xx (Server Errors)
        return exception.response.status_code == 429 or exception.response.status_code >= 500
    return False

class OpenAIProvider(BaseProvider):
    """
    OpenAI API provider implementation using raw async HTTP requests.
    """
    def __init__(self, model_name: str = "gpt-4o", api_key: str | None = None, base_url: str | None = None, **kwargs: Any) -> None:
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        base_url = base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        super().__init__(model_name=model_name, api_key=api_key, base_url=base_url, **kwargs)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_retry_after(fallback=wait_exponential(multiplier=1, min=2, max=10)),
        retry=retry_if_exception(_is_retriable),
        reraise=True
    )
    async def _send_request_with_retry(self, client: httpx.AsyncClient, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Performs async POST request with Tenacity backoff retries."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        
        response = await client.post(url, json=payload, headers=headers, timeout=30.0)
        
        # Raise exception for non-2xx codes, allowing tenacity to retry on 429/5xx status errors
        response.raise_for_status()
        return response.json()

    async def send_prompt(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        async with self.semaphore:
            await self.rate_limiter.acquire()
            if not self.api_key:
                raise ValueError("OpenAI API key must be provided or set via OPENAI_API_KEY environment variable.")
                
            payload = {
                "model": self.model_name,
                "messages": [{"role": "user", "content": prompt}],
                **self.extra_params,
                **kwargs
            }
            
            async with httpx.AsyncClient() as client:
                res_json = await self._send_request_with_retry(client, payload)
                
            choices = res_json.get("choices", [])
            content = ""
            if choices:
                content = choices[0].get("message", {}).get("content") or ""
            usage_data = res_json.get("usage", {})
            
            # Strict validation check
            response = ProviderResponse(
                content=content,
                usage=TokenUsage(
                    prompt_tokens=usage_data.get("prompt_tokens", 0),
                    completion_tokens=usage_data.get("completion_tokens", 0),
                    total_tokens=usage_data.get("total_tokens", 0)
                )
            )
            return response.model_dump()
