import pytest
import httpx
import asyncio
from unittest.mock import AsyncMock, patch
from ag_core.providers.openai_provider import OpenAIProvider
from ag_core.providers.anthropic_provider import AnthropicProvider
from ag_core.providers.grok_provider import GrokProvider

def test_openai_provider_success():
    async def run_test():
        provider = OpenAIProvider(api_key="test-key")
        
        mock_response = httpx.Response(
            status_code=200,
            json={
                "choices": [{"message": {"content": "Hello world!"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15
                }
            },
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        )
        
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            
            response = await provider.send_prompt("Test prompt", temperature=0.7)
            
            assert response["content"] == "Hello world!"
            assert response["usage"]["prompt_tokens"] == 10
            assert response["usage"]["completion_tokens"] == 5
            assert response["usage"]["total_tokens"] == 15
            
            # Verify post parameters
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            assert kwargs["headers"]["Authorization"] == "Bearer test-key"
            assert kwargs["json"]["model"] == "gpt-4o"
            assert kwargs["json"]["temperature"] == 0.7
            assert kwargs["json"]["messages"] == [{"role": "user", "content": "Test prompt"}]
            
    asyncio.run(run_test())

def test_openai_provider_retry_on_500():
    async def run_test():
        provider = OpenAIProvider(api_key="test-key")
        
        response_500 = httpx.Response(status_code=500, request=httpx.Request("POST", "url"))
        response_200 = httpx.Response(
            status_code=200,
            json={
                "choices": [{"message": {"content": "Recovered!"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            },
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        )
        
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = [response_500, response_200]
            
            response = await provider.send_prompt("Test prompt")
            assert response["content"] == "Recovered!"
            assert mock_post.call_count == 2
            
    asyncio.run(run_test())

def test_openai_provider_no_retry_on_401():
    async def run_test():
        provider = OpenAIProvider(api_key="test-key")
        
        response_401 = httpx.Response(status_code=401, request=httpx.Request("POST", "url"))
        
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = response_401
            
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await provider.send_prompt("Test prompt")
                
            assert exc_info.value.response.status_code == 401
            assert mock_post.call_count == 1  # No retries for 401
            
    asyncio.run(run_test())

def test_anthropic_provider_success():
    async def run_test():
        provider = AnthropicProvider(api_key="test-anthropic-key")
        
        mock_response = httpx.Response(
            status_code=200,
            json={
                "content": [{"text": "Hello from Claude"}],
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 8
                }
            },
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            
            response = await provider.send_prompt("Test prompt", max_tokens=2000)
            
            assert response["content"] == "Hello from Claude"
            assert response["usage"]["prompt_tokens"] == 12
            assert response["usage"]["completion_tokens"] == 8
            assert response["usage"]["total_tokens"] == 20
            
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            assert kwargs["headers"]["x-api-key"] == "test-anthropic-key"
            assert kwargs["headers"]["anthropic-version"] == "2023-06-01"
            assert kwargs["json"]["model"] == "claude-3-5-sonnet-20241022"
            assert kwargs["json"]["max_tokens"] == 2000
            
    asyncio.run(run_test())

def test_grok_provider_success():
    async def run_test():
        provider = GrokProvider(api_key="test-grok-key")
        
        mock_response = httpx.Response(
            status_code=200,
            json={
                "choices": [{"message": {"content": "Hello from Grok"}}],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30
                }
            },
            request=httpx.Request("POST", "https://api.x.ai/v1/chat/completions")
        )
        
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            
            response = await provider.send_prompt("Test prompt")
            
            assert response["content"] == "Hello from Grok"
            assert response["usage"]["prompt_tokens"] == 20
            assert response["usage"]["completion_tokens"] == 10
            assert response["usage"]["total_tokens"] == 30
            
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            assert kwargs["headers"]["Authorization"] == "Bearer test-grok-key"
            assert kwargs["json"]["model"] == "grok-2-1212"
            
    asyncio.run(run_test())
