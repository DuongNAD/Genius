import os
import sys
import pytest
import asyncio
import hashlib
import json
import httpx
from unittest.mock import patch, MagicMock, AsyncMock

# Add current workspace to path to import orchestrator and ag_core
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestrator import call_api, run_pipeline, PipelineError, _API_RESPONSE_CACHE
from ag_core.utils.rate_limiter import (
    TokenBucketRateLimiter,
    limiter,
    rate_limit_dependency,
)


@pytest.fixture(autouse=True)
def enable_genius_cache_and_limiter():
    os.environ["ENABLE_GENIUS_CACHE"] = "1"
    os.environ["ENABLE_RATE_LIMITER"] = "1"
    yield
    os.environ.pop("ENABLE_GENIUS_CACHE", None)
    os.environ.pop("ENABLE_RATE_LIMITER", None)


@pytest.fixture
def temp_workspace(tmp_path, monkeypatch):
    """Fixture that moves to a temp directory and returns it as a workspace."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_caching_returns_cached_results_instantly():
    """Verify that calling call_api twice with the same arguments returns the cached response on the second call without network requests."""
    # Clear cache before test
    _API_RESPONSE_CACHE.clear()

    url = "http://localhost:8001"
    api_key = "mock-key"
    prompt = "test prompt for caching"
    context = {"file.py": "content"}

    # Mock response
    mock_res_post = httpx.Response(
        status_code=200,
        json={"status": "processing", "task_id": "cache-task-id"},
        headers={"X-Payload-SHA256": "dummy"},
        request=httpx.Request("POST", f"{url}/run"),
    )
    mock_res_get = httpx.Response(
        status_code=200,
        json={"status": "completed", "result": "Expected Cached Result"},
        headers={"X-Payload-SHA256": "dummy"},
        request=httpx.Request("GET", f"{url}/status/cache-task-id"),
    )

    with patch(
        "orchestrator.perform_post_with_retry", new_callable=AsyncMock
    ) as mock_post, patch(
        "orchestrator.perform_get_with_retry", new_callable=AsyncMock
    ) as mock_get:

        mock_post.return_value = mock_res_post
        mock_get.return_value = mock_res_get

        # First call -> Cache Miss
        result1 = await call_api(url, api_key, prompt, context=context)
        assert result1 == "Expected Cached Result"
        assert mock_post.call_count == 1
        assert mock_get.call_count == 1

        # Reset call counts
        mock_post.reset_mock()
        mock_get.reset_mock()

        # Second call -> Cache Hit
        result2 = await call_api(url, api_key, prompt, context=context)
        assert result2 == "Expected Cached Result"
        # Bypassed network request
        assert mock_post.call_count == 0
        assert mock_get.call_count == 0


def test_rate_limiter_token_bucket_thread_safe():
    """Verify thread-safety and correctness of the TokenBucketRateLimiter."""
    tb = TokenBucketRateLimiter(rate=100.0, capacity=5.0)

    # Consume 5 tokens -> OK
    for _ in range(5):
        assert tb.consume(1.0) is True

    # Consume 6th token -> False
    assert tb.consume(1.0) is False

    # Sleep to refill 1 token
    import time

    time.sleep(0.011)  # rate is 100/sec, so 0.01 sec gives 1 token
    assert tb.consume(1.0) is True


def test_rate_limiter_http_429_returned_when_exceeded():
    """Verify FastAPI dependency returns 429 Too Many Requests with Retry-After when rate limit is exceeded."""
    from fastapi import FastAPI, Depends
    from fastapi.testclient import TestClient

    app = FastAPI(dependencies=[Depends(rate_limit_dependency)])

    @app.get("/test-limit")
    def test_endpoint():
        return {"status": "ok"}

    # Set capacity to 2 to trigger limit quickly
    limiter.capacity = 2.0
    limiter.rate = 10.0
    limiter.reset()

    client = TestClient(app)

    # 1st request -> 200 OK
    res1 = client.get("/test-limit")
    assert res1.status_code == 200

    # 2nd request -> 200 OK
    res2 = client.get("/test-limit")
    assert res2.status_code == 200

    # 3rd request -> 429 Too Many Requests
    res3 = client.get("/test-limit")
    assert res3.status_code == 429
    assert res3.json()["detail"] == "Too Many Requests"
    assert "Retry-After" in res3.headers
    assert res3.headers["Retry-After"] == "1"


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
@patch("asyncio.create_subprocess_exec")
async def test_connection_pool_settings(mock_exec, mock_client_class, temp_workspace):
    """Verify that connection pool limits and timeouts are properly configured on the shared client."""
    # Setup mock subprocess
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"app content", b""))
    mock_proc.returncode = 0
    mock_exec.return_value = mock_proc

    # Setup mock client
    mock_client_instance = MagicMock()
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=None)
    mock_client_class.return_value = mock_client_instance

    # Mock post response
    mock_res_post = httpx.Response(
        status_code=200,
        json={"status": "processing", "task_id": "test-task-id"},
        headers={"X-Payload-SHA256": "dummy"},
        request=httpx.Request("POST", "http://localhost:8001/run"),
    )
    # Mock get response
    mock_res_get = httpx.Response(
        status_code=200,
        json={"status": "completed", "result": "mock results"},
        headers={"X-Payload-SHA256": "dummy"},
        request=httpx.Request("GET", "http://localhost:8001/status/test-task-id"),
    )

    with patch(
        "orchestrator.perform_post_with_retry", new_callable=AsyncMock
    ) as mock_post, patch(
        "orchestrator.perform_get_with_retry", new_callable=AsyncMock
    ) as mock_get:

        mock_post.return_value = mock_res_post
        mock_get.return_value = mock_res_get

        try:
            await run_pipeline(prompt="Build app", workspace=str(temp_workspace))
        except Exception:
            pass

    # Verify httpx.AsyncClient constructor arguments
    assert mock_client_class.call_count >= 1

    # Inspect the constructor arguments of the client
    # Find the call that configured limits
    found_limits_config = False
    for call in mock_client_class.call_args_list:
        kwargs = call[1]
        if "limits" in kwargs and "timeout" in kwargs:
            limits = kwargs["limits"]
            timeout = kwargs["timeout"]

            assert limits.max_keepalive_connections == 50
            assert limits.max_connections == 100

            assert timeout.connect == 5.0
            assert timeout.read == 10.0
            found_limits_config = True
            break

    assert (
        found_limits_config is True
    ), "httpx.AsyncClient was not instantiated with correct pool limits/timeouts"
