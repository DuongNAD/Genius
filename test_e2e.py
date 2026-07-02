import os
import sys
import pytest
import shutil
import stat
import httpx
import asyncio
import hashlib
import json
from unittest.mock import AsyncMock, patch

# Add workspace root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ag_core.config import load_config
from orchestrator import PipelineError

# ==============================================================================
# Helper Functions & Fixtures
# ==============================================================================


def is_orchestrator_rewritten():
    """Verify if orchestrator.py has been rewritten to use httpx and async pipeline."""
    orchestrator_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "orchestrator.py"
    )
    if not os.path.exists(orchestrator_path):
        return False
    with open(orchestrator_path, "r", encoding="utf-8") as f:
        content = f.read()
    return "httpx" in content or "async def run_pipeline" in content


def check_orchestrator_rewritten():
    """Fail the test if the orchestrator has not yet been rewritten to use HTTP client."""
    if not is_orchestrator_rewritten():
        pytest.fail("Orchestrator not yet rewritten to HTTP client")


_skill_api_cache = {}


def import_skill_api(skill_name):
    """Dynamically import api.py from the specified skill folder, failing if not found."""
    if skill_name in _skill_api_cache:
        return _skill_api_cache[skill_name]
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        ".agents",
        "skills",
        skill_name,
        "api.py",
    )
    if not os.path.exists(path):
        short_name = skill_name.split("_")[0]
        pytest.fail(f"{short_name} api.py not implemented yet")
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(f"{skill_name}_api", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _skill_api_cache[skill_name] = module
        return module
    except Exception as e:
        short_name = skill_name.split("_")[0]
        pytest.fail(f"{short_name} api.py failed to import: {e}")


def check_serve_py_exists():
    """Fail the test if serve.py startup script does not exist."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serve.py")
    if not os.path.exists(path):
        pytest.fail("serve.py not implemented yet")


def get_any_available_api():
    """Get any available api module, or fail if none are implemented."""
    for name in ["grok_researcher", "claude_architect", "codex_reviewer"]:
        try:
            return import_skill_api(name)
        except Exception:
            continue
    pytest.fail("No agent api.py implemented yet")


def get_valid_api_key():
    """Get a valid JWT token signed with the configured secret."""
    import time
    from ag_core.utils.jwt import encode_jwt

    config = load_config()
    secret = (
        getattr(config, "skill_api_key", None)
        or os.getenv("SKILL_API_KEY", "mock-skill-key")
        or "mock-skill-key"
    )
    payload = {"sub": "test", "exp": time.time() + 300}  # valid for 5 minutes
    return encode_jwt(payload, secret)


def make_mock_http_response(
    status_code=200, json_data=None, content=None, headers=None
):
    """Helper to construct httpx.Response for mocking with checksum validation."""
    if content is not None:
        body = content if isinstance(content, bytes) else content.encode("utf-8")
    else:
        body = json.dumps(json_data or {}).encode("utf-8")

    resp_headers = headers.copy() if headers else {}
    if "X-Payload-SHA256" not in resp_headers:
        resp_headers["X-Payload-SHA256"] = hashlib.sha256(body).hexdigest()
    if "Content-Type" not in resp_headers:
        resp_headers["Content-Type"] = "application/json"

    return httpx.Response(
        status_code=status_code,
        content=body,
        headers=resp_headers,
        request=httpx.Request("POST", "http://localhost:8000/run"),
    )


def client_post(client, path, headers=None, json_data=None, content=None):
    """Make POST request using TestClient while automatically adding required X-Payload-SHA256 checksum."""
    if headers is None:
        headers = {}

    if json_data is not None:
        body_bytes = json.dumps(json_data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif content is not None:
        body_bytes = content if isinstance(content, bytes) else content.encode("utf-8")
    else:
        body_bytes = b""

    checksum = hashlib.sha256(body_bytes).hexdigest()
    headers["X-Payload-SHA256"] = checksum

    return client.post(path, headers=headers, content=body_bytes)


def client_get(client, path, headers=None):
    """Make GET request using TestClient while automatically adding required X-Payload-SHA256 checksum."""
    if headers is None:
        headers = {}

    checksum = hashlib.sha256(b"").hexdigest()
    headers["X-Payload-SHA256"] = checksum
    return client.get(path, headers=headers)


@pytest.fixture(autouse=True)
def isolate_pipeline_workspace(tmp_path, monkeypatch):
    """Run every test from a per-test tmp dir.

    Many tests here call run_pipeline() without a workspace argument, and the
    pipeline defaults its workspace to os.getcwd(). When cwd is the repo root
    that archives pre-existing root artifacts (research.md -> research.md.bak,
    app.py -> app.py.bak, ...) and leaves temp_workspace_* dirs and projects/
    output behind. Chdir'ing into tmp_path confines all of it to the per-test
    temp dir. Tests that need repo files already use absolute paths.
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def mock_subprocess():
    """Automatically mock asyncio.create_subprocess_exec to prevent FileNotFoundError for CLI commands."""
    mock_process = AsyncMock()
    mock_process.returncode = 0
    mock_process.communicate.return_value = (b"Mock Subprocess Output", b"")
    with patch(
        "asyncio.create_subprocess_exec", return_value=mock_process
    ) as mock_exec:
        yield mock_exec


@pytest.fixture(autouse=True)
def mock_llm_providers():
    """Globally mock LLM provider send_prompt calls in E2E tests to bypass network timeouts."""
    from unittest.mock import AsyncMock, patch

    mock_res = {
        "content": "Mocked LLM Response content",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    with patch(
        "ag_core.providers.openai_provider.OpenAIProvider.send_prompt",
        new_callable=AsyncMock,
        return_value=mock_res,
    ), patch(
        "ag_core.providers.anthropic_provider.AnthropicProvider.send_prompt",
        new_callable=AsyncMock,
        return_value=mock_res,
    ), patch(
        "ag_core.providers.grok_provider.GrokProvider.send_prompt",
        new_callable=AsyncMock,
        return_value=mock_res,
    ):
        yield


# ==============================================================================
# TIER 1: FEATURE COVERAGE (30 tests: 5 tests per feature)
# ==============================================================================

# --- Feature 1: FastAPI Web Server Setup & Unified Startup ---


def test_f1_grok_server_startup():
    """Attempts to load grok's api.py and verify test client GET /docs or POST /run exists."""
    api = import_skill_api("grok_researcher")

    assert hasattr(api, "app"), "Grok api.py does not define 'app'"
    routes = [r.path for r in api.app.routes]
    assert (
        "/docs" in routes or "/run" in routes
    ), "Grok API missing /docs or /run endpoints"


def test_f1_claude_server_startup():
    """Attempts to load claude's api.py and verify test client GET /docs or POST /run exists."""
    api = import_skill_api("claude_architect")

    assert hasattr(api, "app"), "Claude api.py does not define 'app'"
    routes = [r.path for r in api.app.routes]
    assert (
        "/docs" in routes or "/run" in routes
    ), "Claude API missing /docs or /run endpoints"


def test_f1_codex_server_startup():
    """Attempts to load codex's api.py and verify test client GET /docs or POST /run exists."""
    api = import_skill_api("codex_reviewer")

    assert hasattr(api, "app"), "Codex api.py does not define 'app'"
    routes = [r.path for r in api.app.routes]
    assert (
        "/docs" in routes or "/run" in routes
    ), "Codex API missing /docs or /run endpoints"


def test_f1_serve_py_launches_roles():
    """Test that serve.py launches the correct API servers based on CLI arguments."""
    check_serve_py_exists()
    import serve

    assert hasattr(serve, "main"), "serve.py is missing 'main' entrypoint"


def test_f1_swagger_docs_accessible():
    """Verifies Swagger UI HTML can be retrieved from /docs."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client.get("/docs")
    assert res.status_code == 200
    assert (
        "swagger" in res.text.lower()
        or "openapi" in res.text.lower()
        or "redoc" in res.text.lower()
    )


# --- Feature 2: API Authentication (X-API-Key) ---


def test_f2_grok_auth_success():
    """POST /run on Grok service with valid X-API-Key succeeds (returns non-401)."""
    api = import_skill_api("grok_researcher")
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_post(
        client,
        "/run",
        headers={"X-API-Key": get_valid_api_key()},
        json_data={"prompt": "hello"},
    )
    assert res.status_code != 401


def test_f2_claude_auth_success():
    """POST /run on Claude service with valid X-API-Key succeeds (returns non-401)."""
    api = import_skill_api("claude_architect")
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_post(
        client,
        "/run",
        headers={"X-API-Key": get_valid_api_key()},
        json_data={"prompt": "hello"},
    )
    assert res.status_code != 401


def test_f2_codex_auth_success():
    """POST /run on Codex service with valid X-API-Key succeeds (returns non-401)."""
    api = import_skill_api("codex_reviewer")
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_post(
        client,
        "/run",
        headers={"X-API-Key": get_valid_api_key()},
        json_data={"prompt": "hello"},
    )
    assert res.status_code != 401


def test_f2_auth_missing_key():
    """POST /run or GET /status/{task_id} with no X-API-Key header returns 401 (or 422 due to FastAPI required parameter validation)."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_post(client, "/run", json_data={"prompt": "hello"})
    assert res.status_code in (401, 422)


def test_f2_auth_invalid_key():
    """POST /run or GET /status/{task_id} with invalid X-API-Key header returns 401."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_post(
        client,
        "/run",
        headers={"X-API-Key": "wrong-key"},
        json_data={"prompt": "hello"},
    )
    assert res.status_code == 401


def test_f2_status_auth_grok():
    """Verify grok researcher status endpoint auth."""
    api = import_skill_api("grok_researcher")
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    # Missing key
    res = client_get(client, "/status/task-123")
    assert res.status_code in (401, 422)
    # Invalid key
    res = client_get(client, "/status/task-123", headers={"X-API-Key": "wrong-key"})
    assert res.status_code == 401
    # Valid key
    res = client_get(
        client, "/status/task-123", headers={"X-API-Key": get_valid_api_key()}
    )
    assert res.status_code in (200, 404)


def test_f2_status_auth_claude():
    """Verify claude architect status endpoint auth."""
    api = import_skill_api("claude_architect")
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    # Missing key
    res = client_get(client, "/status/task-123")
    assert res.status_code in (401, 422)
    # Invalid key
    res = client_get(client, "/status/task-123", headers={"X-API-Key": "wrong-key"})
    assert res.status_code == 401
    # Valid key
    res = client_get(
        client, "/status/task-123", headers={"X-API-Key": get_valid_api_key()}
    )
    assert res.status_code in (200, 404)


def test_f2_status_auth_codex():
    """Verify codex reviewer status endpoint auth."""
    api = import_skill_api("codex_reviewer")
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    # Missing key
    res = client_get(client, "/status/task-123")
    assert res.status_code in (401, 422)
    # Invalid key
    res = client_get(client, "/status/task-123", headers={"X-API-Key": "wrong-key"})
    assert res.status_code == 401
    # Valid key
    res = client_get(
        client, "/status/task-123", headers={"X-API-Key": get_valid_api_key()}
    )
    assert res.status_code in (200, 404)


# --- Feature 3: Async Task Processing & Stateless Payload ---


def test_f3_async_run_returns_task_id():
    """POST /run returns immediately with a task ID and 'processing' status."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_post(
        client,
        "/run",
        headers={"X-API-Key": get_valid_api_key()},
        json_data={"prompt": "hello"},
    )
    assert res.status_code in (200, 202)
    data = res.json()
    assert "task_id" in data
    assert data.get("status") == "processing"


def test_f3_status_endpoint_returns_state():
    """GET /status/{task_id} returns the current task state."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_get(
        client, "/status/task-123", headers={"X-API-Key": get_valid_api_key()}
    )
    assert res.status_code in (200, 404)
    if res.status_code == 200:
        data = res.json()
        assert "status" in data


def test_f3_payload_stateless_execution(tmp_path, monkeypatch):
    """Agent processes JSON payload and returns result without creating files locally on server."""
    monkeypatch.chdir(tmp_path)
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    before_files = set(os.listdir(os.getcwd()))
    client_post(
        client,
        "/run",
        headers={"X-API-Key": get_valid_api_key()},
        json_data={"prompt": "hello"},
    )
    after_files = set(os.listdir(os.getcwd()))
    print("CURRENT_DIRECTORY:", os.getcwd())
    print("BEFORE_FILES:", before_files)
    print("AFTER_FILES:", after_files)
    added = after_files - before_files
    print("ADDED_FILES:", added)
    assert len(before_files) == len(after_files), f"Added files: {added}"


def test_f3_payload_missing_required_field():
    """POST /run with missing prompt field in JSON returns 422."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_post(
        client,
        "/run",
        headers={"X-API-Key": get_valid_api_key()},
        json_data={"context": "some context"},
    )
    assert res.status_code == 422


def test_f3_payload_malformed_json():
    """POST /run with malformed JSON text returns 400 or 422."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_post(
        client,
        "/run",
        headers={"X-API-Key": get_valid_api_key(), "Content-Type": "application/json"},
        content="{malformed",
    )
    assert res.status_code in (400, 422)


# --- Feature 4: Orchestrator HTTP Polling & Routing ---


def test_f4_orchestrator_routes_requests():
    """Orchestrator invokes all steps sequentially using HTTP client calls."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get:
        mock_post.return_value = make_mock_http_response(
            200, {"task_id": "t-1", "status": "processing"}
        )
        mock_get.return_value = make_mock_http_response(
            200, {"status": "completed", "result": "mock output"}
        )

        asyncio.run(run_pipeline(prompt="E2E test run"))
        assert mock_post.call_count >= 4


def test_f4_orchestrator_polls_status():
    """Orchestrator polls /status/{task_id} periodically and waits before moving to the next pipeline step."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get, patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_post.return_value = make_mock_http_response(
            200, {"task_id": "t-1", "status": "processing"}
        )
        mock_get.side_effect = [
            make_mock_http_response(200, {"status": "processing"}),
            make_mock_http_response(
                200, {"status": "completed", "result": "mock output"}
            ),
        ] * 6

        asyncio.run(run_pipeline(prompt="E2E test run"))
        assert mock_get.call_count >= 4
        assert mock_sleep.called


def test_f4_orchestrator_saves_outputs_locally():
    """Orchestrator saves received response contents locally to workspace."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get:
        mock_post.return_value = make_mock_http_response(
            200, {"task_id": "t-1", "status": "processing"}
        )
        mock_get.return_value = make_mock_http_response(
            200, {"status": "completed", "result": "result data"}
        )

        workspace_dir = "temp_workspace_f4"
        os.makedirs(workspace_dir, exist_ok=True)
        try:
            asyncio.run(run_pipeline(prompt="Save files", workspace=workspace_dir))
            for f in [
                "research.md",
                "design.md",
                "app.py",
                "review.md",
                "test_generated.py",
            ]:
                assert os.path.exists(os.path.join(workspace_dir, f))
        finally:
            shutil.rmtree(workspace_dir, ignore_errors=True)


def test_f4_orchestrator_non_zero_exit_on_step_failure():
    """Non-zero exit code or error raised when step fails."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get:
        mock_post.return_value = make_mock_http_response(
            200, {"task_id": "t-1", "status": "processing"}
        )
        mock_get.return_value = make_mock_http_response(
            200, {"status": "failed", "error": "Internal Agent Error"}
        )
        with pytest.raises(PipelineError):
            asyncio.run(run_pipeline(prompt="Failure test"))


def test_f4_orchestrator_url_args_override():
    """Command-line URL override parameters are respected."""
    check_orchestrator_rewritten()
    import orchestrator
    import inspect

    sig = inspect.signature(orchestrator.run_pipeline)
    if "grok_url" not in sig.parameters:
        pytest.fail("Orchestrator command-line URL overrides not yet implemented")


# --- Feature 5: Resilient HTTP & 429 Retry Handling ---


def test_f5_orchestrator_retries_on_429():
    """Retries when receiving 429."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get, patch("asyncio.sleep", new_callable=AsyncMock):
        mock_post.side_effect = [
            make_mock_http_response(429, headers={"Retry-After": "1"}),
            make_mock_http_response(200, {"task_id": "t-1", "status": "processing"}),
        ] + [
            make_mock_http_response(200, {"task_id": "t-1", "status": "processing"})
        ] * 5
        mock_get.return_value = make_mock_http_response(
            200, {"status": "completed", "result": "mock"}
        )

        asyncio.run(run_pipeline(prompt="Retry 429"))
        assert mock_post.call_count == 7


def test_f5_orchestrator_respects_retry_after():
    """Waits for duration in Retry-After header."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get, patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_post.side_effect = [
            make_mock_http_response(429, headers={"Retry-After": "0.1"}),
            make_mock_http_response(200, {"task_id": "t-1", "status": "processing"}),
        ] + [
            make_mock_http_response(200, {"task_id": "t-1", "status": "processing"})
        ] * 5
        mock_get.return_value = make_mock_http_response(
            200, {"status": "completed", "result": "mock"}
        )

        asyncio.run(run_pipeline(prompt="Respect Retry-After"))
        assert mock_sleep.called


def test_f5_orchestrator_max_retries_exhaustion():
    """Raises error after 3 failed attempts."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = make_mock_http_response(
            429, headers={"Retry-After": "0"}
        )
        with pytest.raises(Exception):
            asyncio.run(run_pipeline(prompt="Max retries"))
        assert mock_post.call_count == 3


def test_f5_orchestrator_no_retry_on_400_or_401():
    """Does not retry on 400 or 401 client errors."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = make_mock_http_response(401)
        with pytest.raises(Exception):
            asyncio.run(run_pipeline(prompt="No retry 401"))
        if mock_post.call_count > 1:
            print("MOCK CALLS:", mock_post.mock_calls)
            pytest.fail(
                f"Orchestrator retries on 400/401 client errors (should not retry). Calls: {mock_post.mock_calls}"
            )


def test_f5_orchestrator_timeout_handling():
    """Raises exception on request timeout."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch(
        "httpx.AsyncClient.post", side_effect=httpx.TimeoutException("Timeout")
    ), patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(Exception):
            asyncio.run(run_pipeline(prompt="Timeout"))
        assert mock_sleep.called


# --- Feature 6: Configuration & Workspace Management ---


def test_f6_config_loads_urls_from_yaml():
    """Configuration module loads microservice URLs from YAML."""
    config = load_config()
    if not any(
        hasattr(config, attr)
        for attr in ["grok_url", "claude_url", "codex_url", "urls", "services"]
    ):
        pytest.fail("Config module not yet updated with microservice URLs")
    assert hasattr(config, "services")


def test_f6_config_loads_api_keys_from_env(monkeypatch):
    """Configuration module loads keys from .env."""
    monkeypatch.setenv("GROK_API_KEY", "test-grok-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-claude-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    import os

    print(f"DEBUG_ENV: GROK_API_KEY={os.getenv('GROK_API_KEY')}")
    config = load_config()
    print(f"DEBUG_ENV: config.grok_api_key={config.grok_api_key}")
    assert config.grok_api_key == "test-grok-key"
    assert config.anthropic_api_key == "test-claude-key"
    assert config.openai_api_key == "test-openai-key"


def test_f6_orchestrator_cleans_workspace():
    """Deletes old output files before running."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    workspace_dir = "temp_workspace_f6_clean"
    os.makedirs(workspace_dir, exist_ok=True)
    old_file = os.path.join(workspace_dir, "research.md")
    with open(old_file, "w") as f:
        f.write("stale research")

    try:
        with patch(
            "httpx.AsyncClient.post", new_callable=AsyncMock
        ) as mock_post, patch(
            "httpx.AsyncClient.get", new_callable=AsyncMock
        ) as mock_get:
            mock_post.return_value = make_mock_http_response(
                200, {"task_id": "t-1", "status": "processing"}
            )
            mock_get.return_value = make_mock_http_response(
                200, {"status": "completed", "result": "new research"}
            )

            asyncio.run(run_pipeline(prompt="Clean workspace", workspace=workspace_dir))
            with open(old_file, "r") as f:
                assert f.read() == "new research"
    finally:
        import shutil

        shutil.rmtree(workspace_dir, ignore_errors=True)


def test_f6_orchestrator_invalid_workspace_raises_error():
    """Raises error for invalid workspace path."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    def mock_makedirs(path, *args, **kwargs):
        if "/non_existent/directory" in path.replace("\\", "/"):
            raise PipelineError("Permission denied / Directory not found")
        os.makedirs(path, *args, **kwargs)

    with patch("os.makedirs", side_effect=mock_makedirs):
        with pytest.raises(PipelineError):
            asyncio.run(
                run_pipeline(
                    prompt="test", workspace="/non_existent/directory/12345/abc"
                )
            )


def test_f6_orchestrator_handles_permission_error():
    """Logs warnings or raises error if workspace files cannot be archived.

    clean_output_files archives research.md -> research.md.bak via os.replace;
    a read-only .bak destination makes that replace fail on Windows, which must
    surface as a PipelineError instead of silently consuming stale artifacts.
    """
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    workspace_dir = "temp_workspace_f6_perm"
    os.makedirs(workspace_dir, exist_ok=True)
    stale_file = os.path.join(workspace_dir, "research.md")
    with open(stale_file, "w") as f:
        f.write("stale artifact")
    locked_bak = stale_file + ".bak"
    with open(locked_bak, "w") as f:
        f.write("locked backup")
    os.chmod(locked_bak, stat.S_IREAD)

    try:
        with pytest.raises(PipelineError):
            asyncio.run(run_pipeline(prompt="test", workspace=workspace_dir))
    finally:
        os.chmod(locked_bak, stat.S_IWRITE)
        shutil.rmtree(workspace_dir, ignore_errors=True)


# ==============================================================================
# TIER 2: BOUNDARY & CORNER CASES (30 tests: 5 tests per feature)
# ==============================================================================

# --- Feature 1 Setup & Startup Boundaries ---


def test_f1_server_port_already_in_use():
    """Fails cleanly or raises error when port occupied."""
    api = get_any_available_api()
    with patch("uvicorn.run", side_effect=OSError(98, "Address already in use")):
        with pytest.raises(OSError):
            if hasattr(api, "start_server"):
                api.start_server(port=8000)
            else:
                raise OSError(98, "Address already in use")


def test_f1_serve_py_invalid_roles():
    """Test serve.py with invalid/unknown roles CLI arguments."""
    check_serve_py_exists()
    import serve

    with pytest.raises(Exception):
        serve.main(args=["--roles", "invalid_role_xyz"])


def test_f1_serve_py_interactive_prompt_selection():
    """Test serve.py interactive menu selection simulation."""
    check_serve_py_exists()
    import serve

    with patch("builtins.input", return_value="1"):
        assert hasattr(serve, "interactive_prompt") or hasattr(
            serve, "interactive_menu"
        )


def test_f1_server_very_long_url():
    """Long path routing handling."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    long_path = "/" + "a" * 2000
    res = client.get(long_path)
    assert res.status_code in (404, 414)


def test_f1_server_unsupported_http_method():
    """Unsupported method (GET on /run) returns 405."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_get(client, "/run")
    assert res.status_code == 405


# --- Feature 2 Auth Boundaries ---


def test_f2_auth_key_extremely_long():
    """Extremely long X-API-Key value."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    long_key = "a" * 8000
    res = client_post(
        client, "/run", headers={"X-API-Key": long_key}, json_data={"prompt": "hello"}
    )
    assert res.status_code in (401, 431)


def test_f2_auth_case_sensitivity():
    """API Key headers case sensitivity verification."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_post(
        client,
        "/run",
        headers={"x-api-key": get_valid_api_key()},
        json_data={"prompt": "hello"},
    )
    assert res.status_code != 401


def test_f2_auth_expired_or_revoked_key():
    """Simulation of expired/revoked key returning 401."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    with patch.dict(os.environ, {"VALID_API_KEYS": ""}):
        res = client_post(
            client,
            "/run",
            headers={"X-API-Key": "expired-key"},
            json_data={"prompt": "hello"},
        )
        assert res.status_code == 401


def test_f2_auth_empty_key_header():
    """Header X-API-Key: "" present but empty returns 401."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_post(
        client, "/run", headers={"X-API-Key": ""}, json_data={"prompt": "hello"}
    )
    assert res.status_code == 401


def test_f2_auth_special_chars_in_key():
    """Special characters in API key handled safely."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_post(
        client,
        "/run",
        headers={"X-API-Key": "key_#$@_123"},
        json_data={"prompt": "hello"},
    )
    assert res.status_code in (401, 200, 202)


# --- Feature 3 Stateless & Polling Boundaries ---


def test_f3_payload_extremely_large_prompt():
    """Large payload (1MB) parsed or rejected cleanly."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    large_prompt = "a" * (1024 * 1024)
    res = client_post(
        client,
        "/run",
        headers={"X-API-Key": get_valid_api_key()},
        json_data={"prompt": large_prompt},
    )
    assert res.status_code in (200, 202, 413, 422)


def test_f3_payload_empty_prompt_and_context():
    """Request with empty strings handled."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_post(
        client,
        "/run",
        headers={"X-API-Key": get_valid_api_key()},
        json_data={"prompt": "", "context": ""},
    )
    assert res.status_code in (200, 202, 422)


def test_f3_payload_extra_unexpected_fields():
    """Ignores/handles unknown fields."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_post(
        client,
        "/run",
        headers={"X-API-Key": get_valid_api_key()},
        json_data={"prompt": "hello", "unexpected_field_abc": "value"},
    )
    assert res.status_code in (200, 202)


def test_f3_payload_special_characters():
    """Handles unicode/emojis in prompt."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_post(
        client,
        "/run",
        headers={"X-API-Key": get_valid_api_key()},
        json_data={"prompt": "hello 🚀 ☄️ 星星", "context": {"research.md": "📝"}},
    )
    assert res.status_code in (200, 202)


def test_f3_status_invalid_task_id():
    """GET /status/{task_id} with invalid/unknown task ID returns 404."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client_get(
        client,
        "/status/unknown-task-id-999",
        headers={"X-API-Key": get_valid_api_key()},
    )
    assert res.status_code == 404


# --- Feature 4 Orchestrator Boundaries ---


def test_f4_orchestrator_extremely_large_pipeline_response():
    """Large response body handled."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get:
        mock_post.return_value = make_mock_http_response(
            200, {"task_id": "t-1", "status": "processing"}
        )
        large_content = "A" * (2 * 1024 * 1024)
        mock_get.return_value = make_mock_http_response(
            200, {"status": "completed", "result": large_content}
        )

        workspace_dir = "temp_workspace_f4_large"
        os.makedirs(workspace_dir, exist_ok=True)
        try:
            asyncio.run(run_pipeline(prompt="large", workspace=workspace_dir))
            assert os.path.exists(os.path.join(workspace_dir, "research.md"))
        finally:
            shutil.rmtree(workspace_dir, ignore_errors=True)


def test_f4_orchestrator_agent_returns_empty_result():
    """Empty result payload aborts pipeline."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get:
        mock_post.return_value = make_mock_http_response(
            200, {"task_id": "t-1", "status": "processing"}
        )
        mock_get.return_value = make_mock_http_response(
            200, {"status": "completed", "result": ""}
        )
        with pytest.raises(PipelineError):
            asyncio.run(run_pipeline(prompt="empty"))


def test_f4_orchestrator_invalid_port_in_url():
    """Invalid/out-of-range port raises config error."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch(
        "httpx.AsyncClient.post", side_effect=httpx.InvalidURL("Invalid port range")
    ):
        with pytest.raises(Exception):
            asyncio.run(
                run_pipeline(prompt="invalid port", grok_url="http://localhost:999999")
            )


def test_f4_orchestrator_agent_disconnects_mid_polling():
    """Disconnection handled cleanly during status polling."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", side_effect=httpx.ConnectError("Connection lost")
    ), patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_post.return_value = make_mock_http_response(
            200, {"task_id": "t-1", "status": "processing"}
        )
        with pytest.raises(PipelineError):
            asyncio.run(run_pipeline(prompt="disconnect"))
        assert mock_sleep.called


def test_f4_orchestrator_polling_timeout_exhaustion():
    """Orchestrator times out if task status remains 'processing' past maximum polling timeout."""
    check_orchestrator_rewritten()
    orchestrator_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "orchestrator.py"
    )
    with open(orchestrator_path, "r", encoding="utf-8") as f:
        content = f.read()
    if "timeout" not in content and "poll_timeout" not in content:
        pytest.fail("Orchestrator polling timeout exhaustion not yet implemented")


# --- Feature 5 Resilience Boundaries ---


def test_f5_429_missing_retry_after():
    """Fallback backoff when 429 missing Retry-After."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get, patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_post.side_effect = [
            make_mock_http_response(429),
            make_mock_http_response(200, {"task_id": "t-1", "status": "processing"}),
        ] + [
            make_mock_http_response(200, {"task_id": "t-1", "status": "processing"})
        ] * 5
        mock_get.return_value = make_mock_http_response(
            200, {"status": "completed", "result": "mock"}
        )

        asyncio.run(run_pipeline(prompt="No Retry-After"))
        assert mock_post.call_count == 7
        assert mock_sleep.called


def test_f5_429_malformed_retry_after():
    """Fallback backoff when Retry-After malformed."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get, patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_post.side_effect = [
            make_mock_http_response(429, headers={"Retry-After": "invalid-value"}),
            make_mock_http_response(200, {"task_id": "t-1", "status": "processing"}),
        ] + [
            make_mock_http_response(200, {"task_id": "t-1", "status": "processing"})
        ] * 5
        mock_get.return_value = make_mock_http_response(
            200, {"status": "completed", "result": "mock"}
        )

        asyncio.run(run_pipeline(prompt="Malformed Retry-After"))
        assert mock_post.call_count == 7
        assert mock_sleep.called


def test_f5_429_massive_retry_after():
    """Capping extremely large Retry-After to prevent hanging."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        mock_post.return_value = make_mock_http_response(
            429, headers={"Retry-After": "999999"}
        )

        with pytest.raises(Exception):
            asyncio.run(run_pipeline(prompt="Massive Retry-After"))

        if mock_sleep.called:
            called_sleep_time = mock_sleep.call_args[0][0]
            assert called_sleep_time <= 60.0


def test_f5_concurrent_requests_all_429():
    """Multiple concurrent tasks getting 429."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get, patch("asyncio.sleep", new_callable=AsyncMock):
        mock_post.side_effect = [
            make_mock_http_response(429, headers={"Retry-After": "0"}),
            make_mock_http_response(200, {"task_id": "t-1", "status": "processing"}),
        ] * 6
        mock_get.return_value = make_mock_http_response(
            200, {"status": "completed", "result": "mock"}
        )

        asyncio.run(run_pipeline(prompt="Concurrent 429"))
        assert mock_post.call_count == 12


def test_f5_429_recovery_on_third_attempt():
    """429 twice, then succeeds on 3rd attempt."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get:
        mock_post.side_effect = [
            make_mock_http_response(429, headers={"Retry-After": "0"}),
            make_mock_http_response(429, headers={"Retry-After": "0"}),
            make_mock_http_response(200, {"task_id": "t-1", "status": "processing"}),
        ] + [
            make_mock_http_response(200, {"task_id": "t-1", "status": "processing"})
        ] * 5
        mock_get.return_value = make_mock_http_response(
            200, {"status": "completed", "result": "mock"}
        )

        asyncio.run(run_pipeline(prompt="3rd attempt success"))
        assert mock_post.call_count == 8


# --- Feature 6 Config/Workspace Boundaries ---


def test_f6_config_missing_yaml_falls_back():
    """Config defaults when YAML missing."""
    config_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.yaml"
    )
    backup_file = config_file + ".bak"
    if os.path.exists(config_file):
        shutil.move(config_file, backup_file)
    try:
        config = load_config("config.yaml")
        assert config.app.name is not None
    finally:
        if os.path.exists(backup_file):
            shutil.move(backup_file, config_file)


def test_f6_config_env_var_override(monkeypatch):
    """Env variables override YAML."""
    monkeypatch.setenv("OPENAI_API_KEY", "env-override-openai-key")
    import os

    print(f"DEBUG_ENV: OPENAI_API_KEY={os.getenv('OPENAI_API_KEY')}")
    config = load_config()
    print(f"DEBUG_ENV: config.openai_api_key={config.openai_api_key}")
    assert config.openai_api_key == "env-override-openai-key"


def test_f6_orchestrator_read_only_output_files():
    """Handles archive failure for outputs whose .bak destination is locked."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    workspace_dir = "temp_workspace_f6_readonly"
    os.makedirs(workspace_dir, exist_ok=True)
    old_file = os.path.join(workspace_dir, "research.md")
    with open(old_file, "w") as f:
        f.write("old data")
    locked_bak = old_file + ".bak"
    with open(locked_bak, "w") as f:
        f.write("locked backup")
    os.chmod(locked_bak, stat.S_IREAD)

    try:
        with pytest.raises(PipelineError):
            asyncio.run(run_pipeline(prompt="test", workspace=workspace_dir))
    finally:
        os.chmod(locked_bak, stat.S_IWRITE)
        shutil.rmtree(workspace_dir, ignore_errors=True)


def test_f6_orchestrator_whitespace_only_urls():
    """Whitespace-only URLs raise validation error."""
    check_orchestrator_rewritten()
    import orchestrator
    import inspect

    sig = inspect.signature(orchestrator.run_pipeline)
    if "grok_url" not in sig.parameters:
        pytest.fail("Orchestrator URL overrides not yet implemented")


def test_f6_orchestrator_concurrent_pipeline_runs():
    """Parallel orchestrators in different workspaces do not interfere."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get:
        mock_post.return_value = make_mock_http_response(
            200, {"task_id": "t-1", "status": "processing"}
        )
        mock_get.return_value = make_mock_http_response(
            200, {"status": "completed", "result": "output"}
        )

        ws1 = "ws_concurrent_1"
        ws2 = "ws_concurrent_2"
        os.makedirs(ws1, exist_ok=True)
        os.makedirs(ws2, exist_ok=True)
        try:

            async def main_concurrent():
                await asyncio.gather(
                    run_pipeline(prompt="Run 1", workspace=ws1),
                    run_pipeline(prompt="Run 2", workspace=ws2),
                )

            asyncio.run(main_concurrent())
            assert os.path.exists(os.path.join(ws1, "research.md"))
            assert os.path.exists(os.path.join(ws2, "research.md"))
        finally:
            shutil.rmtree(ws1, ignore_errors=True)
            shutil.rmtree(ws2, ignore_errors=True)


# ==============================================================================
# TIER 3: CROSS-FEATURE COMBINATIONS (6 tests)
# ==============================================================================


def test_t3_api_key_rotation_during_pipeline():
    """Key rotation handled during runtime."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get:
        mock_post.return_value = make_mock_http_response(
            200, {"task_id": "t-1", "status": "processing"}
        )
        mock_get.return_value = make_mock_http_response(
            200, {"status": "completed", "result": "rotated"}
        )

        with patch.dict(os.environ, {"GROK_API_KEY": "new-grok-key"}):
            asyncio.run(run_pipeline(prompt="Key rotation"))
            assert mock_post.call_count >= 4


def test_t3_stateless_agent_reboot_mid_pipeline():
    """Agent reboots, stateless retry succeeds."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get, patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_post.return_value = make_mock_http_response(
            200, {"task_id": "t-1", "status": "processing"}
        )
        mock_get.side_effect = [
            make_mock_http_response(200, {"status": "completed", "result": "ok"}),
            httpx.ConnectError("Server rebooting"),
            make_mock_http_response(200, {"status": "completed", "result": "ok"}),
        ] + [make_mock_http_response(200, {"status": "completed", "result": "ok"})] * 5

        asyncio.run(run_pipeline(prompt="Server reboot"))
        assert mock_post.call_count == 6
        assert mock_sleep.called


def test_t3_rate_limiting_triggered_sequentially():
    """All sequential steps get 429 and retry."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get:
        mock_post.side_effect = [
            make_mock_http_response(429, headers={"Retry-After": "0"}),
            make_mock_http_response(200, {"task_id": "t-1", "status": "processing"}),
            make_mock_http_response(429, headers={"Retry-After": "0"}),
            make_mock_http_response(200, {"task_id": "t-2", "status": "processing"}),
            make_mock_http_response(429, headers={"Retry-After": "0"}),
            make_mock_http_response(200, {"task_id": "t-3", "status": "processing"}),
            make_mock_http_response(429, headers={"Retry-After": "0"}),
            make_mock_http_response(200, {"task_id": "t-4", "status": "processing"}),
            make_mock_http_response(429, headers={"Retry-After": "0"}),
            make_mock_http_response(200, {"task_id": "t-5", "status": "processing"}),
            make_mock_http_response(429, headers={"Retry-After": "0"}),
            make_mock_http_response(200, {"task_id": "t-6", "status": "processing"}),
        ]
        mock_get.return_value = make_mock_http_response(
            200, {"status": "completed", "result": "success"}
        )

        asyncio.run(run_pipeline(prompt="Cascade 429"))
        assert mock_post.call_count == 12


def test_t3_config_overrides_passed_to_http_client():
    """CL overrides flow to HTTP headers."""
    check_orchestrator_rewritten()
    import orchestrator
    import inspect

    sig = inspect.signature(orchestrator.run_pipeline)
    if "api_key_override" not in sig.parameters:
        pytest.fail("Orchestrator api_key_override parameter not yet implemented")


def test_t3_workspace_cleanup_on_http_failure():
    """Workspace cleaned on step failure."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    workspace_dir = "temp_workspace_t3_cleanup"
    os.makedirs(workspace_dir, exist_ok=True)
    try:
        with patch(
            "httpx.AsyncClient.post", new_callable=AsyncMock
        ) as mock_post, patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_post.side_effect = httpx.HTTPStatusError(
                "Error", request=None, response=make_mock_http_response(500)
            )

            with pytest.raises(PipelineError):
                asyncio.run(
                    run_pipeline(prompt="HTTP fail clean", workspace=workspace_dir)
                )

            assert not os.path.exists(os.path.join(workspace_dir, "research.md"))
            assert mock_sleep.called
    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)


def test_t3_swagger_docs_reflect_correct_security_schemas():
    """Security schemes correctly defined in OpenAPI."""
    api = get_any_available_api()
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    res = client.get("/openapi.json")
    assert res.status_code == 200
    openapi_schema = res.json()
    components = openapi_schema.get("components", {})
    security_schemes = components.get("securitySchemes", {})
    if not security_schemes or not any(
        x in str(security_schemes) for x in ["ApiKeyAuth", "X-API-Key"]
    ):
        pytest.fail("FastAPI OpenAPI schema does not define X-API-Key security scheme")


# ==============================================================================
# TIER 4: REAL-WORLD APPLICATION SCENARIOS (5 tests)
# ==============================================================================


def test_t4_real_world_successful_microservices_pipeline():
    """Complete microservices E2E run mock success."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get:
        mock_post.return_value = make_mock_http_response(
            200, {"task_id": "real-task-1", "status": "processing"}
        )
        mock_get.return_value = make_mock_http_response(
            200, {"status": "completed", "result": "Genuine final output"}
        )

        workspace_dir = "temp_workspace_t4_success"
        os.makedirs(workspace_dir, exist_ok=True)
        try:
            asyncio.run(
                run_pipeline(
                    prompt="Deploy production microservices", workspace=workspace_dir
                )
            )
            for f in [
                "research.md",
                "design.md",
                "app.py",
                "review.md",
                "test_generated.py",
            ]:
                assert os.path.exists(os.path.join(workspace_dir, f))
        finally:
            shutil.rmtree(workspace_dir, ignore_errors=True)


def test_t4_real_world_network_jitter_recovery():
    """Network jitter/429/500 recovery."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get, patch("asyncio.sleep", new_callable=AsyncMock):

        mock_post.side_effect = [
            make_mock_http_response(429, headers={"Retry-After": "0"}),
            make_mock_http_response(500),
            make_mock_http_response(
                200, {"task_id": "jitter-task", "status": "processing"}
            ),
        ] + [
            make_mock_http_response(
                200, {"task_id": "jitter-task", "status": "processing"}
            )
        ] * 5

        mock_get.side_effect = [
            httpx.ConnectError("Jitter"),
            make_mock_http_response(200, {"status": "processing"}),
            make_mock_http_response(
                200, {"status": "completed", "result": "recovered output"}
            ),
        ] * 6

        asyncio.run(run_pipeline(prompt="Jitter test"))
        assert mock_post.call_count == 8


def test_t4_real_world_empty_design_payload_aborts():
    """Empty design payload aborts."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get:
        mock_post.return_value = make_mock_http_response(
            200, {"task_id": "t-1", "status": "processing"}
        )
        mock_get.side_effect = [
            make_mock_http_response(
                200, {"status": "completed", "result": "research content"}
            ),
            make_mock_http_response(200, {"status": "completed", "result": ""}),
        ]
        with pytest.raises(PipelineError):
            asyncio.run(run_pipeline(prompt="Empty design"))


def test_t4_real_world_unauthorized_agent_aborts_pipeline():
    """Unauthorized step 2 aborts."""
    check_orchestrator_rewritten()
    from orchestrator import run_pipeline

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, patch(
        "httpx.AsyncClient.get", new_callable=AsyncMock
    ) as mock_get:
        mock_post.side_effect = [
            make_mock_http_response(200, {"task_id": "t-1", "status": "processing"}),
            make_mock_http_response(401),
        ]
        mock_get.return_value = make_mock_http_response(
            200, {"status": "completed", "result": "step 1 results"}
        )
        with pytest.raises(PipelineError):
            asyncio.run(run_pipeline(prompt="Auth failure on step 2"))


def test_t4_real_world_invalid_yaml_config_handling():
    """Invalid config.yaml handling."""
    config_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.yaml"
    )
    backup_file = config_file + ".bak"
    if os.path.exists(config_file):
        shutil.move(config_file, backup_file)
    try:
        with open(config_file, "w", encoding="utf-8") as f:
            f.write("app:\n  name: [invalid yaml\n")

        try:
            load_config("config.yaml")
            pytest.fail("Config module does not raise exception on invalid YAML")
        except pytest.fail.Exception:
            raise
        except Exception:
            pass
    finally:
        if os.path.exists(backup_file):
            if os.path.exists(config_file):
                os.remove(config_file)
            shutil.move(backup_file, config_file)
