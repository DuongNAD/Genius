# test_devops_security_challenger.py
import os
import sys
import time
import pytest
import hashlib
import json
import httpx
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

# Ensure workspace root is in python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ag_core.utils.jwt import encode_jwt
from test_e2e import get_valid_api_key


@pytest.fixture(autouse=True)
def mock_providers():
    from unittest.mock import AsyncMock, patch

    mock_openai_res = {
        "content": "Mocked Security Agent Audit Report Output",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    mock_anthropic_res = {
        "content": "Mocked DevOps Agent Deployment Configuration Output",
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }
    with patch(
        "ag_core.providers.openai_provider.OpenAIProvider.send_prompt",
        new_callable=AsyncMock,
        return_value=mock_openai_res,
    ), patch(
        "ag_core.providers.anthropic_provider.AnthropicProvider.send_prompt",
        new_callable=AsyncMock,
        return_value=mock_anthropic_res,
    ):
        yield


# Helper to load apps
def get_security_app():
    from test_e2e_phase5 import get_security_agent_app

    return get_security_agent_app()


def get_devops_app():
    from test_e2e_phase5 import get_devops_agent_app

    return get_devops_agent_app()


# Helper to construct a request with X-Payload-SHA256
def make_headers(
    jwt_token,
    payload_dict=None,
    raw_body=None,
    include_checksum=True,
    checksum_override=None,
    token_key="X-API-Key",
    bearer_style=False,
):
    headers = {}
    if jwt_token:
        if bearer_style:
            headers["Authorization"] = f"Bearer {jwt_token}"
        else:
            headers["X-API-Key"] = jwt_token

    if payload_dict is not None:
        body = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
    elif raw_body is not None:
        body = raw_body if isinstance(raw_body, bytes) else raw_body.encode("utf-8")
    else:
        body = b""

    if include_checksum:
        if checksum_override:
            headers["X-Payload-SHA256"] = checksum_override
        else:
            headers["X-Payload-SHA256"] = hashlib.sha256(body).hexdigest()

    headers["Content-Type"] = "application/json"
    return headers, body


# ==============================================================================
# AUTHENTICATION & HEADER TESTS
# ==============================================================================


@pytest.mark.parametrize(
    "get_app,role", [(get_security_app, "security"), (get_devops_app, "devops")]
)
def test_auth_missing_token(get_app, role):
    """Verify that endpoints reject requests without a token with HTTP 401."""
    app = get_app()
    client = TestClient(app)
    headers, body = make_headers(jwt_token=None, payload_dict={"prompt": "test"})

    # POST /run
    res = client.post("/run", content=body, headers=headers)
    assert res.status_code == 401
    assert "detail" in res.json()

    # GET /status/123
    res_status = client.get(
        "/status/123-abc", headers={"X-Payload-SHA256": hashlib.sha256(b"").hexdigest()}
    )
    assert res_status.status_code == 401


@pytest.mark.parametrize(
    "get_app,role", [(get_security_app, "security"), (get_devops_app, "devops")]
)
def test_auth_invalid_token_format(get_app, role):
    """Verify that endpoints reject requests with malformed tokens with HTTP 401."""
    app = get_app()
    client = TestClient(app)
    headers, body = make_headers(
        jwt_token="not.a.jwt.token", payload_dict={"prompt": "test"}
    )

    res = client.post("/run", content=body, headers=headers)
    assert res.status_code == 401


@pytest.mark.parametrize(
    "get_app,role", [(get_security_app, "security"), (get_devops_app, "devops")]
)
def test_auth_wrong_signature(get_app, role):
    """Verify that tokens signed with the wrong secret are rejected with HTTP 401."""
    app = get_app()
    client = TestClient(app)

    # Encode with wrong key
    wrong_token = encode_jwt(
        {"sub": "orchestrator", "exp": time.time() + 300}, "wrong-secret-key"
    )

    headers, body = make_headers(jwt_token=wrong_token, payload_dict={"prompt": "test"})
    res = client.post("/run", content=body, headers=headers)
    assert res.status_code == 401


@pytest.mark.parametrize(
    "get_app,role", [(get_security_app, "security"), (get_devops_app, "devops")]
)
def test_auth_expired_token(get_app, role):
    """Verify that expired tokens are rejected with HTTP 401."""
    app = get_app()
    client = TestClient(app)

    # Token expired 10 minutes ago
    expired_token = encode_jwt(
        {"sub": "orchestrator", "exp": time.time() - 600}, "test-key"
    )

    headers, body = make_headers(
        jwt_token=expired_token, payload_dict={"prompt": "test"}
    )
    res = client.post("/run", content=body, headers=headers)
    assert res.status_code == 401


@pytest.mark.parametrize(
    "get_app,role", [(get_security_app, "security"), (get_devops_app, "devops")]
)
def test_auth_bearer_casing_and_spaces(get_app, role):
    """Verify that Authorization: Bearer is parsed case-insensitively and tolerates multiple spaces."""
    app = get_app()
    client = TestClient(app)

    # Prepare identical payload bytes and exact checksum
    payload = {"prompt": "test"}
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    checksum = hashlib.sha256(body).hexdigest()

    # Test case-insensitivity: "bearer <token>"
    jwt_token1 = get_valid_api_key()
    headers = {
        "Authorization": f"bearer {jwt_token1}",
        "X-Payload-SHA256": checksum,
        "Content-Type": "application/json",
    }
    res = client.post("/run", content=body, headers=headers)
    assert res.status_code == 200
    assert "task_id" in res.json()

    # Test multiple spaces: "Bearer      <token>"
    jwt_token2 = get_valid_api_key()
    headers = {
        "Authorization": f"Bearer      {jwt_token2}",
        "X-Payload-SHA256": checksum,
        "Content-Type": "application/json",
    }
    res = client.post("/run", content=body, headers=headers)
    assert res.status_code == 200

    # Test raw token without "Bearer ": "Authorization: <token>"
    jwt_token3 = get_valid_api_key()
    headers = {
        "Authorization": jwt_token3,
        "X-Payload-SHA256": checksum,
        "Content-Type": "application/json",
    }
    res = client.post("/run", content=body, headers=headers)
    assert res.status_code == 200


# ==============================================================================
# INTEGRITY & CHECKSUM TESTS
# ==============================================================================


@pytest.mark.parametrize(
    "get_app,role", [(get_security_app, "security"), (get_devops_app, "devops")]
)
def test_checksum_missing(get_app, role):
    """Verify that requests missing the X-Payload-SHA256 header are rejected with HTTP 400."""
    app = get_app()
    client = TestClient(app)
    jwt_token = get_valid_api_key()

    headers, body = make_headers(
        jwt_token=jwt_token, payload_dict={"prompt": "test"}, include_checksum=False
    )
    res = client.post("/run", content=body, headers=headers)
    assert res.status_code == 400
    assert "Missing X-Payload-SHA256 header" in res.json().get("detail", "")


@pytest.mark.parametrize(
    "get_app,role", [(get_security_app, "security"), (get_devops_app, "devops")]
)
def test_checksum_mismatch(get_app, role):
    """Verify that requests with mismatched checksums are rejected with HTTP 400."""
    app = get_app()
    client = TestClient(app)
    jwt_token = get_valid_api_key()

    headers, body = make_headers(
        jwt_token=jwt_token,
        payload_dict={"prompt": "test"},
        checksum_override="wrongchecksumvalue",
    )
    res = client.post("/run", content=body, headers=headers)
    assert res.status_code == 400
    assert "Checksum mismatch" in res.json().get("detail", "")


@pytest.mark.parametrize(
    "get_app,role", [(get_security_app, "security"), (get_devops_app, "devops")]
)
def test_checksum_response_header(get_app, role):
    """Verify that responses contain the correct X-Payload-SHA256 header matching the response body."""
    app = get_app()
    client = TestClient(app)
    jwt_token = get_valid_api_key()

    headers, body = make_headers(jwt_token=jwt_token, payload_dict={"prompt": "test"})
    res = client.post("/run", content=body, headers=headers)
    assert res.status_code == 200

    expected_sum = hashlib.sha256(res.content).hexdigest()
    assert res.headers.get("X-Payload-SHA256") == expected_sum


# ==============================================================================
# PAYLOAD VALIDATION & EDGE CASES
# ==============================================================================


@pytest.mark.parametrize(
    "get_app,role", [(get_security_app, "security"), (get_devops_app, "devops")]
)
def test_payload_empty_prompt(get_app, role):
    """Verify that an empty prompt string is rejected with HTTP 400."""
    app = get_app()
    client = TestClient(app)
    jwt_token = get_valid_api_key()

    headers, body = make_headers(jwt_token=jwt_token, payload_dict={"prompt": ""})
    res = client.post("/run", content=body, headers=headers)
    assert res.status_code == 400
    assert "Prompt cannot be empty" in res.json().get("detail", "")


@pytest.mark.parametrize(
    "get_app,role", [(get_security_app, "security"), (get_devops_app, "devops")]
)
def test_payload_blank_prompt(get_app, role):
    """Verify that a prompt consisting only of whitespace is rejected with HTTP 400."""
    app = get_app()
    client = TestClient(app)
    jwt_token = get_valid_api_key()

    headers, body = make_headers(jwt_token=jwt_token, payload_dict={"prompt": "   "})
    res = client.post("/run", content=body, headers=headers)
    assert res.status_code == 400
    assert "Prompt cannot be empty" in res.json().get("detail", "")


@pytest.mark.parametrize(
    "get_app,role", [(get_security_app, "security"), (get_devops_app, "devops")]
)
def test_payload_missing_prompt_field(get_app, role):
    """Verify that a body missing the prompt field is rejected with HTTP 422."""
    app = get_app()
    client = TestClient(app)
    jwt_token = get_valid_api_key()

    # Model validation occurs at Pydantic level, should raise 422
    headers, body = make_headers(jwt_token=jwt_token, payload_dict={"context": {}})
    res = client.post("/run", content=body, headers=headers)
    assert res.status_code == 422


@pytest.mark.parametrize(
    "get_app,role", [(get_security_app, "security"), (get_devops_app, "devops")]
)
def test_payload_invalid_json(get_app, role):
    """Verify that malformed JSON is rejected with HTTP 422/400."""
    app = get_app()
    client = TestClient(app)
    jwt_token = get_valid_api_key()

    bad_json = '{"prompt": "test", "context": '
    headers, body = make_headers(jwt_token=jwt_token, raw_body=bad_json)
    res = client.post("/run", content=body, headers=headers)
    # FastAPI returns 422 or 400 for malformed json parsing error
    assert res.status_code in (400, 422)


# ==============================================================================
# ROUTING & SLASH COMMAND CONFIGURATIONS
# ==============================================================================


def test_routing_table_mapping():
    """Verify that serve.py and orchestrator.py map the required slash commands to ports 8005 and 8006."""
    import serve
    import orchestrator

    # 1. serve.py mappings
    assert serve.ROUTING_TABLE["/security"] == ("security", 8005)
    assert serve.ROUTING_TABLE["/audit"] == ("security", 8005)
    assert serve.ROUTING_TABLE["/security-audit"] == ("security", 8005)
    assert serve.ROUTING_TABLE["/deploy"] == ("devops", 8006)

    # 2. orchestrator.py mappings
    assert orchestrator.ROUTING_TABLE["/security"] == ("security", "audit.md")
    assert orchestrator.ROUTING_TABLE["/audit"] == ("security", "audit.md")
    assert orchestrator.ROUTING_TABLE["/security-audit"] == ("security", "audit.md")
    assert orchestrator.ROUTING_TABLE["/deploy"] == ("devops", "deploy.md")


@pytest.mark.asyncio
async def test_orchestrator_routing_execution(tmp_path):
    """Verify orchestrator runs /security and /deploy commands via smart routing to ports 8005/8006."""
    import orchestrator

    posted_urls = []

    def mock_post(url, *args, **kwargs):
        posted_urls.append(url)
        # Mock successful task registration
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.content = b'{"task_id": "mocked-task-id-123"}'
        resp.headers = httpx.Headers(
            {"X-Payload-SHA256": hashlib.sha256(resp.content).hexdigest()}
        )
        resp.json = lambda: {"task_id": "mocked-task-id-123"}
        return resp

    def mock_get(url, *args, **kwargs):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.content = b'{"status": "completed", "result": "mocked output content"}'
        resp.headers = httpx.Headers(
            {"X-Payload-SHA256": hashlib.sha256(resp.content).hexdigest()}
        )
        resp.json = lambda: {"status": "completed", "result": "mocked output content"}
        return resp

    # Test /security routing
    with patch("httpx.AsyncClient.post", side_effect=mock_post), patch(
        "httpx.AsyncClient.get", side_effect=mock_get
    ):

        await orchestrator.run_pipeline("/security audit code", workspace=str(tmp_path))
        assert len(posted_urls) == 1
        assert "8005" in posted_urls[0] or "security" in posted_urls[0]

    # Test /deploy routing
    posted_urls.clear()
    with patch("httpx.AsyncClient.post", side_effect=mock_post), patch(
        "httpx.AsyncClient.get", side_effect=mock_get
    ):

        await orchestrator.run_pipeline("/deploy service", workspace=str(tmp_path))
        assert len(posted_urls) == 1
        assert (
            "8006" in posted_urls[0]
            or "devops" in posted_urls[0]
            or "deploy" in posted_urls[0]
        )


# ==============================================================================
# RATE LIMITING & RETRY TESTS
# ==============================================================================


@pytest.mark.parametrize(
    "get_app,role", [(get_security_app, "security"), (get_devops_app, "devops")]
)
def test_rate_limiter_active_and_retry_after(get_app, role):
    """Verify that endpoints return HTTP 429 and Retry-After header under load with ENABLE_RATE_LIMITER=true."""
    app = get_app()
    client = TestClient(app)

    # Import limiter instance and reset it
    from ag_core.utils.rate_limiter import limiter

    # Store original values
    orig_rate = limiter.rate
    orig_capacity = limiter.capacity

    try:
        limiter.rate = 0.001
        limiter.capacity = 2.0
        limiter.reset()

        # Run with ENABLE_RATE_LIMITER set
        with patch.dict(
            os.environ, {"ENABLE_RATE_LIMITER": "true", "PYTEST_CURRENT_TEST": ""}
        ):
            # Consume the bucket (capacity is 2)
            headers1, body1 = make_headers(
                jwt_token=get_valid_api_key(), payload_dict={"prompt": "test"}
            )
            res1 = client.post("/run", content=body1, headers=headers1)
            assert res1.status_code == 200

            headers2, body2 = make_headers(
                jwt_token=get_valid_api_key(), payload_dict={"prompt": "test"}
            )
            res2 = client.post("/run", content=body2, headers=headers2)
            assert res2.status_code == 200

            # The 3rd request should be rate-limited
            headers3, body3 = make_headers(
                jwt_token=get_valid_api_key(), payload_dict={"prompt": "test"}
            )
            res_limited = client.post("/run", content=body3, headers=headers3)
            assert res_limited.status_code == 429
            assert res_limited.headers.get("Retry-After") == "1"
            assert "Too Many Requests" in res_limited.json().get("detail", "")
    finally:
        limiter.rate = orig_rate
        limiter.capacity = orig_capacity
        limiter.reset()


@pytest.mark.asyncio
async def test_orchestrator_tenacity_retry_on_429():
    """Verify orchestrator call_api handles 429 rate limit errors with tenacity retries."""
    import orchestrator

    call_count = 0

    def mock_post(url, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        resp = MagicMock(spec=httpx.Response)
        if call_count < 3:
            # Return 429 with Retry-After header
            resp.status_code = 429
            resp.content = b'{"detail": "Rate limit exceeded"}'
            resp.headers = httpx.Headers(
                {
                    "X-Payload-SHA256": hashlib.sha256(resp.content).hexdigest(),
                    "Retry-After": "0.1",
                }
            )
            resp.raise_for_status = lambda: (_ for _ in ()).throw(
                httpx.HTTPStatusError("Rate Limit", request=MagicMock(), response=resp)
            )
        else:
            resp.status_code = 200
            resp.content = b'{"task_id": "retry-task-123"}'
            resp.headers = httpx.Headers(
                {"X-Payload-SHA256": hashlib.sha256(resp.content).hexdigest()}
            )
            resp.json = lambda: {"task_id": "retry-task-123"}
            resp.raise_for_status = lambda: None
        return resp

    def mock_get(url, *args, **kwargs):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.content = b'{"status": "completed", "result": "success"}'
        resp.headers = httpx.Headers(
            {"X-Payload-SHA256": hashlib.sha256(resp.content).hexdigest()}
        )
        resp.json = lambda: {"status": "completed", "result": "success"}
        resp.raise_for_status = lambda: None
        return resp

    with patch("httpx.AsyncClient.post", side_effect=mock_post), patch(
        "httpx.AsyncClient.get", side_effect=mock_get
    ), patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        # Make the call
        result = await orchestrator.call_api(
            "http://localhost:8005", "api_key", "test prompt"
        )
        assert result == "success"
        assert call_count == 3
        # Should sleep twice before the third success call
        assert mock_sleep.call_count >= 2
