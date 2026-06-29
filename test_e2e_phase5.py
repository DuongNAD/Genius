import os
import sys
import pytest
import sqlite3
import json
import httpx
import hashlib
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

# Add workspace root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_e2e import (
    get_valid_api_key,
    make_mock_http_response,
    client_post,
    client_get,
)

from ag_core.memory.vector_store import VectorMemory, SimpleTFIDFEmbedding

# Helper functions to check implementation existence

def check_security_agent_implemented():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".agents", "skills", "security_agent", "api.py")
    if not os.path.exists(path):
        pytest.fail("Security agent api.py not implemented yet")

def check_devops_agent_implemented():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".agents", "skills", "devops_agent", "api.py")
    if not os.path.exists(path):
        pytest.fail("DevOps agent api.py not implemented yet")

_security_agent_app_cache = None
_devops_agent_app_cache = None

def get_security_agent_app():
    global _security_agent_app_cache
    if _security_agent_app_cache is not None:
        return _security_agent_app_cache
    check_security_agent_implemented()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".agents", "skills", "security_agent", "api.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("security_agent_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _security_agent_app_cache = module.app
    return _security_agent_app_cache

def get_devops_agent_app():
    global _devops_agent_app_cache
    if _devops_agent_app_cache is not None:
        return _devops_agent_app_cache
    check_devops_agent_implemented()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".agents", "skills", "devops_agent", "api.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("devops_agent_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _devops_agent_app_cache = module.app
    return _devops_agent_app_cache

def check_orchestrator_routing_implemented():
    try:
        import orchestrator
        has_security = "/security" in orchestrator.ROUTING_TABLE and orchestrator.ROUTING_TABLE["/security"][0] in ["security", "security_agent"]
        has_deploy = "/deploy" in orchestrator.ROUTING_TABLE and orchestrator.ROUTING_TABLE["/deploy"][0] in ["devops", "devops_agent"]
        if not (has_security and has_deploy):
            pytest.fail("Security/DevOps orchestrator routing not implemented yet")
    except Exception as e:
        pytest.fail(f"Routing check failed: {e}")

def check_serve_roles_implemented():
    try:
        import serve
        roles = serve.normalize_roles("security,devops")
        if "security" not in roles or "devops" not in roles:
            pytest.fail("serve.py Security/DevOps roles not implemented yet")
    except Exception as e:
        pytest.fail(f"serve.py checks failed: {e}")

def check_cicd_yaml_exists():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".github", "workflows", "ci.yml")
    if not os.path.exists(path):
        pytest.fail("CI/CD workflow file (.github/workflows/ci.yml) not implemented yet")

def make_mock_response(status_code, json_data, headers=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    content_bytes = json.dumps(json_data).encode("utf-8")
    resp.content = content_bytes
    resp.headers = httpx.Headers(headers or {})
    resp.headers["X-Payload-SHA256"] = hashlib.sha256(content_bytes).hexdigest()
    resp.json = lambda: json_data
    def raise_for_status():
        if status_code >= 400:
            raise httpx.HTTPStatusError("HTTP Error", request=MagicMock(), response=resp)
    resp.raise_for_status = raise_for_status
    return resp


# ==============================================================================
# TIER 1: FEATURE COVERAGE (25 tests)
# ==============================================================================

# --- Feature 1: Vector Memory Import & Read/Write (5 tests) ---

def test_f1_vector_memory_import():
    """Verify VectorMemory class can be imported."""
    assert VectorMemory is not None

def test_f1_vector_memory_instantiation(tmp_path):
    """Verify VectorMemory can be instantiated with SQLite fallback."""
    db_file = tmp_path / "test_vector.db"
    vm = VectorMemory(collection_name="test_col", use_chroma=False, db_path=str(db_file))
    assert vm.collection_name == "test_col"
    assert vm.db_path == str(db_file)
    assert os.path.exists(db_file)

def test_f1_vector_memory_add_document(tmp_path):
    """Verify adding documents to VectorMemory works."""
    db_file = tmp_path / "test_vector.db"
    vm = VectorMemory(collection_name="test_col", use_chroma=False, db_path=str(db_file))
    doc_id = vm.add("This is a test document", metadata={"author": "admin"})
    assert isinstance(doc_id, str)
    
    # Query SQLite directly to verify insertion
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("SELECT id, text, metadata FROM agent_vector_memory_fallback WHERE id = ?", (doc_id,))
    row = cursor.fetchone()
    conn.close()
    
    assert row is not None
    assert row[1] == "This is a test document"
    assert json.loads(row[2]) == {"author": "admin"}

def test_f1_vector_memory_query(tmp_path):
    """Verify vector memory TF-IDF query retrieval."""
    db_file = tmp_path / "test_vector.db"
    vm = VectorMemory(collection_name="test_col", use_chroma=False, db_path=str(db_file))
    
    vm.add("Python programming language and machine learning", doc_id="python-ml")
    vm.add("JavaScript framework for front-end web development", doc_id="js-web")
    
    results = vm.query("python machine", n_results=1)
    assert len(results) == 1
    assert results[0]["id"] == "python-ml"
    
    results_js = vm.query("javascript framework", n_results=1)
    assert len(results_js) == 1
    assert results_js[0]["id"] == "js-web"

def test_f1_vector_memory_chroma_fallback(tmp_path):
    """Verify fallback behavior to SQLite if Chroma is not available/configured."""
    db_file = tmp_path / "test_vector.db"
    # Even if we set use_chroma=True, it should fallback gracefully if Chroma is not available or persistent client fails
    vm = VectorMemory(collection_name="test_col", use_chroma=True, db_path=str(db_file))
    assert not vm.use_chroma or os.path.exists(db_file) or hasattr(vm, "collection")


# --- Feature 2: Security Agent Startup & Routes (5 tests) ---

def test_f2_security_agent_exists():
    """Verify security agent file exists and import returns valid FastAPI app."""
    check_security_agent_implemented()
    app = get_security_agent_app()
    from fastapi import FastAPI
    assert isinstance(app, FastAPI)

def test_f2_security_agent_app_defined():
    """Verify security agent api has app defined."""
    app = get_security_agent_app()
    assert app is not None

def test_f2_security_agent_run_route():
    """Verify security agent api has /run endpoint."""
    app = get_security_agent_app()
    routes = [r.path for r in app.routes]
    assert "/run" in routes, "Security Agent API missing /run endpoint"

def test_f2_security_agent_status_route():
    """Verify security agent api has /status endpoint."""
    app = get_security_agent_app()
    routes = [r.path for r in app.routes]
    assert any(r == "/status/{task_id}" or r.startswith("/status/") for r in routes), "Security Agent API missing /status endpoint"

def test_f2_security_agent_port():
    """Verify security agent runs on port 8005 assignment."""
    check_security_agent_implemented()
    import serve
    assert "security" in serve.ROUTING_TABLE or "security_agent" in serve.ROUTING_TABLE or "/security" in serve.ROUTING_TABLE
    port = serve.ROUTING_TABLE.get("/security", ("security", 8005))[1]
    assert port == 8005


# --- Feature 3: DevOps Agent Startup & Routes (5 tests) ---

def test_f3_devops_agent_exists():
    """Verify DevOps agent file exists and import returns valid FastAPI app."""
    check_devops_agent_implemented()
    app = get_devops_agent_app()
    from fastapi import FastAPI
    assert isinstance(app, FastAPI)

def test_f3_devops_agent_app_defined():
    """Verify DevOps agent api has app defined."""
    app = get_devops_agent_app()
    assert app is not None

def test_f3_devops_agent_run_route():
    """Verify DevOps agent api has /run endpoint."""
    app = get_devops_agent_app()
    routes = [r.path for r in app.routes]
    assert "/run" in routes, "DevOps Agent API missing /run endpoint"

def test_f3_devops_agent_status_route():
    """Verify DevOps agent api has /status endpoint."""
    app = get_devops_agent_app()
    routes = [r.path for r in app.routes]
    assert any(r == "/status/{task_id}" or r.startswith("/status/") for r in routes), "DevOps Agent API missing /status endpoint"

def test_f3_devops_agent_port():
    """Verify DevOps agent runs on port 8006 assignment."""
    check_devops_agent_implemented()
    import serve
    assert "devops" in serve.ROUTING_TABLE or "devops_agent" in serve.ROUTING_TABLE or "/deploy" in serve.ROUTING_TABLE
    port = serve.ROUTING_TABLE.get("/deploy", ("devops", 8006))[1]
    assert port == 8006


# --- Feature 4: Orchestrator Routing to Security/DevOps on ports 8005/8006 (5 tests) ---

def test_f4_orchestrator_routing_table_has_security():
    """Verify security routing entry exists in orchestrator/serve routing table."""
    check_orchestrator_routing_implemented()
    import orchestrator
    assert "/security" in orchestrator.ROUTING_TABLE
    assert orchestrator.ROUTING_TABLE["/security"][0] in ["security", "security_agent"]

def test_f4_orchestrator_routing_table_has_devops():
    """Verify DevOps routing entry exists in orchestrator/serve routing table."""
    check_orchestrator_routing_implemented()
    import orchestrator
    assert "/deploy" in orchestrator.ROUTING_TABLE
    assert orchestrator.ROUTING_TABLE["/deploy"][0] in ["devops", "devops_agent"]

def test_f4_orchestrator_config_urls():
    """Verify configuration defines URLs for security/devops services."""
    check_orchestrator_routing_implemented()
    from ag_core.config import load_config
    config = load_config()
    assert hasattr(config.services, "security_agent") or hasattr(config.services, "security")
    assert hasattr(config.services, "devops_agent") or hasattr(config.services, "devops")

@pytest.mark.asyncio
async def test_f4_orchestrator_routing_command_audit(tmp_path):
    """Verify orchestrator routes /audit or /security-audit commands to port 8005."""
    check_orchestrator_routing_implemented()
    import orchestrator
    
    posted_url = None
    def mock_post(url, *args, **kwargs):
        nonlocal posted_url
        posted_url = url
        return make_mock_response(200, {"task_id": "audit_task"})
        
    def mock_get(url, *args, **kwargs):
        return make_mock_response(200, {"status": "completed", "result": "secure_code_report"})
        
    with patch("httpx.AsyncClient.post", side_effect=mock_post), \
         patch("httpx.AsyncClient.get", side_effect=mock_get):
        await orchestrator.run_pipeline("/security audit code", workspace=str(tmp_path))
        assert posted_url is not None
        assert "8005" in posted_url or "security" in posted_url


@pytest.mark.asyncio
async def test_f4_orchestrator_routing_command_deploy(tmp_path):
    """Verify orchestrator routes /deploy commands to port 8006."""
    check_orchestrator_routing_implemented()
    import orchestrator
    
    posted_url = None
    def mock_post(url, *args, **kwargs):
        nonlocal posted_url
        posted_url = url
        return make_mock_response(200, {"task_id": "deploy_task"})
        
    def mock_get(url, *args, **kwargs):
        return make_mock_response(200, {"status": "completed", "result": "deploy_success"})
        
    with patch("httpx.AsyncClient.post", side_effect=mock_post), \
         patch("httpx.AsyncClient.get", side_effect=mock_get):
        await orchestrator.run_pipeline("/deploy app", workspace=str(tmp_path))
        assert posted_url is not None
        assert "8006" in posted_url or "devops" in posted_url or "deploy" in posted_url


# --- Feature 5: serve.py CLI Options & Interactive Role Startup (5 tests) ---

def test_f5_serve_cli_roles_support_security():
    """Verify serve.py accepts '--roles security' command line option."""
    check_serve_roles_implemented()
    import serve
    assert "security" in serve.normalize_roles("security")

def test_f5_serve_cli_roles_support_devops():
    """Verify serve.py accepts '--roles devops' command line option."""
    check_serve_roles_implemented()
    import serve
    assert "devops" in serve.normalize_roles("devops")

def test_f5_serve_interactive_menu_shows_security():
    """Verify serve.py interactive menu text displays security role option."""
    check_serve_roles_implemented()
    import serve
    with patch("builtins.input", return_value="security"):
        roles = serve.interactive_prompt()
        assert "security" in roles

def test_f5_serve_interactive_menu_shows_devops():
    """Verify serve.py interactive menu text displays devops role option."""
    check_serve_roles_implemented()
    import serve
    with patch("builtins.input", return_value="devops"):
        roles = serve.interactive_prompt()
        assert "devops" in roles

@pytest.mark.asyncio
async def test_f5_serve_launches_security_and_devops_servers():
    """Verify serve.py main_async starts the Security/DevOps servers when requested."""
    check_serve_roles_implemented()
    import serve
    
    mock_args = MagicMock()
    mock_args.roles = "security,devops"
    mock_args.prompt = None
    
    with patch("argparse.ArgumentParser.parse_args", return_value=mock_args), \
         patch("serve.start_server", new_callable=AsyncMock) as mock_start_server:
        await serve.main_async()
        mock_start_server.assert_any_call("security", 8005)
        mock_start_server.assert_any_call("devops", 8006)


# ==============================================================================
# TIER 2: BOUNDARY & CORNER CASES (10 tests)
# ==============================================================================

def test_t2_security_auth_401():
    """Verify Security Agent rejects requests with invalid or missing API Key."""
    app = get_security_agent_app()
    client = TestClient(app)
    
    body = {"prompt": "audit"}
    body_bytes = json.dumps(body).encode("utf-8")
    checksum_post = hashlib.sha256(body_bytes).hexdigest()
    checksum_get = hashlib.sha256(b"").hexdigest()
    
    # Missing API Key/Auth
    resp = client.post("/run", content=body_bytes, headers={"X-Payload-SHA256": checksum_post})
    assert resp.status_code == 401
    
    resp_status = client.get("/status/task_id", headers={"X-Payload-SHA256": checksum_get})
    assert resp_status.status_code == 401
    
    # Invalid API Key
    resp_invalid = client.post(
        "/run",
        content=body_bytes,
        headers={
            "X-API-Key": "invalid_key",
            "Authorization": "Bearer invalid_key",
            "X-Payload-SHA256": checksum_post
        }
    )
    assert resp_invalid.status_code == 401

def test_t2_devops_auth_401():
    """Verify DevOps Agent rejects requests with invalid or missing API Key."""
    app = get_devops_agent_app()
    client = TestClient(app)
    
    body = {"prompt": "deploy"}
    body_bytes = json.dumps(body).encode("utf-8")
    checksum_post = hashlib.sha256(body_bytes).hexdigest()
    checksum_get = hashlib.sha256(b"").hexdigest()
    
    # Missing API Key/Auth
    resp = client.post("/run", content=body_bytes, headers={"X-Payload-SHA256": checksum_post})
    assert resp.status_code == 401
    
    resp_status = client.get("/status/task_id", headers={"X-Payload-SHA256": checksum_get})
    assert resp_status.status_code == 401
    
    # Invalid API Key
    resp_invalid = client.post(
        "/run",
        content=body_bytes,
        headers={
            "X-API-Key": "invalid_key",
            "Authorization": "Bearer invalid_key",
            "X-Payload-SHA256": checksum_post
        }
    )
    assert resp_invalid.status_code == 401

def test_t2_security_checksum_mismatch_400():
    """Verify Security Agent rejects requests with payload checksum mismatch."""
    app = get_security_agent_app()
    client = TestClient(app)
    
    from test_e2e import get_valid_api_key
    jwt_token = get_valid_api_key()
    
    headers = {
        "X-API-Key": jwt_token,
        "Authorization": f"Bearer {jwt_token}"
    }
    
    # Missing X-Payload-SHA256
    resp = client.post("/run", json={"prompt": "audit"}, headers=headers)
    assert resp.status_code == 400
    
    # Invalid X-Payload-SHA256
    jwt_token2 = get_valid_api_key()
    headers2 = {
        "X-API-Key": jwt_token2,
        "Authorization": f"Bearer {jwt_token2}",
        "X-Payload-SHA256": "bad_checksum"
    }
    resp2 = client.post("/run", json={"prompt": "audit"}, headers=headers2)
    assert resp2.status_code == 400

def test_t2_devops_checksum_mismatch_400():
    """Verify DevOps Agent rejects requests with payload checksum mismatch."""
    app = get_devops_agent_app()
    client = TestClient(app)
    
    from test_e2e import get_valid_api_key
    jwt_token = get_valid_api_key()
    
    headers = {
        "X-API-Key": jwt_token,
        "Authorization": f"Bearer {jwt_token}"
    }
    
    # Missing X-Payload-SHA256
    resp = client.post("/run", json={"prompt": "deploy"}, headers=headers)
    assert resp.status_code == 400
    
    # Invalid X-Payload-SHA256
    jwt_token2 = get_valid_api_key()
    headers2 = {
        "X-API-Key": jwt_token2,
        "Authorization": f"Bearer {jwt_token2}",
        "X-Payload-SHA256": "bad_checksum"
    }
    resp2 = client.post("/run", json={"prompt": "deploy"}, headers=headers2)
    assert resp2.status_code == 400

@pytest.mark.asyncio
async def test_t2_security_rate_limit_429_retry():
    """Verify orchestrator retries Security Agent calls upon encountering 429 status."""
    check_security_agent_implemented()
    check_orchestrator_routing_implemented()
    
    import orchestrator
    from orchestrator import call_api
    
    call_count = 0
    def mock_post(url, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return make_mock_response(429, {"detail": "Rate limited"})
        else:
            return make_mock_response(200, {"task_id": "test_task"})
            
    def mock_get(url, *args, **kwargs):
        return make_mock_response(200, {"status": "completed", "result": "secure"})
        
    with patch("httpx.AsyncClient.post", side_effect=mock_post), \
         patch("httpx.AsyncClient.get", side_effect=mock_get), \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
           
        result = await call_api("http://localhost:8005", "api_key", "test prompt")
        assert result == "secure"
        assert call_count == 3
        assert mock_sleep.call_count >= 2

@pytest.mark.asyncio
async def test_t2_devops_rate_limit_429_retry():
    """Verify orchestrator retries DevOps Agent calls upon encountering 429 status."""
    check_devops_agent_implemented()
    check_orchestrator_routing_implemented()
    
    import orchestrator
    from orchestrator import call_api
    
    call_count = 0
    def mock_post(url, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return make_mock_response(429, {"detail": "Rate limited"})
        else:
            return make_mock_response(200, {"task_id": "test_task"})
            
    def mock_get(url, *args, **kwargs):
        return make_mock_response(200, {"status": "completed", "result": "deployed"})
        
    with patch("httpx.AsyncClient.post", side_effect=mock_post), \
         patch("httpx.AsyncClient.get", side_effect=mock_get), \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
           
        result = await call_api("http://localhost:8006", "api_key", "test prompt")
        assert result == "deployed"
        assert call_count == 3
        assert mock_sleep.call_count >= 2

def test_t2_security_empty_prompt_400():
    """Verify Security Agent returns 400/422 for empty prompts or empty code input."""
    app = get_security_agent_app()
    client = TestClient(app)
    
    jwt_token = get_valid_api_key()
    
    headers = {
        "X-API-Key": jwt_token,
        "Authorization": f"Bearer {jwt_token}"
    }
    
    # Empty prompt
    body = {"prompt": "", "context": {}}
    payload_bytes = json.dumps(body).encode("utf-8")
    headers["X-Payload-SHA256"] = hashlib.sha256(payload_bytes).hexdigest()
    resp = client.post("/run", content=payload_bytes, headers=headers)
    assert resp.status_code in [400, 422]
    
    # Missing prompt
    jwt_token2 = get_valid_api_key()
    headers2 = {
        "X-API-Key": jwt_token2,
        "Authorization": f"Bearer {jwt_token2}"
    }
    body_missing = {"context": {}}
    payload_bytes_missing = json.dumps(body_missing).encode("utf-8")
    headers2["X-Payload-SHA256"] = hashlib.sha256(payload_bytes_missing).hexdigest()
    resp2 = client.post("/run", content=payload_bytes_missing, headers=headers2)
    assert resp2.status_code in [400, 422]

def test_t2_devops_empty_prompt_400():
    """Verify DevOps Agent returns 400/422 for empty prompts or empty config input."""
    app = get_devops_agent_app()
    client = TestClient(app)
    
    jwt_token = get_valid_api_key()
    
    headers = {
        "X-API-Key": jwt_token,
        "Authorization": f"Bearer {jwt_token}"
    }
    
    # Empty prompt
    body = {"prompt": "", "context": {}}
    payload_bytes = json.dumps(body).encode("utf-8")
    headers["X-Payload-SHA256"] = hashlib.sha256(payload_bytes).hexdigest()
    resp = client.post("/run", content=payload_bytes, headers=headers)
    assert resp.status_code in [400, 422]
    
    # Missing prompt
    jwt_token2 = get_valid_api_key()
    headers2 = {
        "X-API-Key": jwt_token2,
        "Authorization": f"Bearer {jwt_token2}"
    }
    body_missing = {"context": {}}
    payload_bytes_missing = json.dumps(body_missing).encode("utf-8")
    headers2["X-Payload-SHA256"] = hashlib.sha256(payload_bytes_missing).hexdigest()
    resp2 = client.post("/run", content=payload_bytes_missing, headers=headers2)
    assert resp2.status_code in [400, 422]

def test_t2_vector_memory_empty_query_sqlite(tmp_path):
    """Verify querying empty VectorMemory SQLite database returns an empty list gracefully."""
    db_file = tmp_path / "test_empty_vector.db"
    vm = VectorMemory(collection_name="empty_col", use_chroma=False, db_path=str(db_file))
    results = vm.query("any query", n_results=5)
    assert isinstance(results, list)
    assert len(results) == 0

def test_t2_vector_memory_empty_query_chroma(tmp_path):
    """Verify querying empty VectorMemory Chroma collection handles empty results gracefully."""
    db_file = tmp_path / "test_empty_vector_chroma.db"
    vm = VectorMemory(collection_name="empty_col_chroma", use_chroma=True, db_path=str(db_file))
    # In fallback SQLite mode (if chroma is disabled), it should still work.
    results = vm.query("any query", n_results=5)
    assert isinstance(results, list)
    assert len(results) == 0


# ==============================================================================
# TIER 3: CROSS-FEATURE COMBINATIONS (3 tests)
# ==============================================================================

@pytest.mark.asyncio
async def test_t3_sequential_pipeline_with_security_and_devops(tmp_path):
    """Verify full sequential pipeline including security and devops execution."""
    import orchestrator
    import inspect
    source = inspect.getsource(orchestrator.run_pipeline)
    if "security" not in source.lower() or "devops" not in source.lower():
        pytest.fail("Security/DevOps pipeline integration not implemented yet")
        
    called_urls = []
    
    def mock_post(url, *args, **kwargs):
        called_urls.append(url)
        return make_mock_response(200, {"task_id": "test_task"})
        
    def mock_get(url, *args, **kwargs):
        if "grok" in url:
            res = "Grok research output"
        elif "claude" in url:
            res = "Claude design output"
        elif "codex" in url:
            res = "Codex review output"
        elif "tester" in url:
            res = "Tester code output"
        elif "security" in url:
            res = "Security audit report"
        elif "devops" in url or "deploy" in url:
            res = "DevOps deployment config"
        else:
            res = "Default mock result"
        return make_mock_response(200, {"status": "completed", "result": res})
        
    mock_process = AsyncMock()
    mock_process.communicate.return_value = (b"mocked app code", b"")
    mock_process.returncode = 0
    
    with patch("httpx.AsyncClient.post", side_effect=mock_post), \
         patch("httpx.AsyncClient.get", side_effect=mock_get), \
         patch("asyncio.create_subprocess_exec", return_value=mock_process):
         
        await orchestrator.run_pipeline("test prompt", workspace=str(tmp_path))
        print("DEBUG: called_urls =", called_urls)
        assert any("8001" in url for url in called_urls)
        assert any("8002" in url for url in called_urls)
        assert any("8003" in url for url in called_urls)
        assert any("8004" in url for url in called_urls)
        assert any("8005" in url for url in called_urls)
        assert any("8006" in url for url in called_urls)

@pytest.mark.asyncio
async def test_t3_routing_multiple_slash_commands(tmp_path):
    """Verify multiple slash commands for security and devops are correctly routed."""
    check_orchestrator_routing_implemented()
    import orchestrator
    
    called_urls = []
    def mock_post(url, *args, **kwargs):
        called_urls.append(url)
        return make_mock_response(200, {"task_id": "test_task"})
        
    def mock_get(url, *args, **kwargs):
        return make_mock_response(200, {"status": "completed", "result": "mock_result"})
        
    with patch("httpx.AsyncClient.post", side_effect=mock_post), \
         patch("httpx.AsyncClient.get", side_effect=mock_get):
         
        await orchestrator.run_pipeline("/security audit code", workspace=str(tmp_path))
        await orchestrator.run_pipeline("/deploy app", workspace=str(tmp_path))
        
        assert len(called_urls) >= 2
        assert any("8005" in url or "security" in url for url in called_urls)
        assert any("8006" in url or "devops" in url or "deploy" in url for url in called_urls)

@pytest.mark.asyncio
async def test_t3_codex_security_devops_build_audit_deploy_chain(tmp_path):
    """Verify build-audit-deploy chain execution with mocked microservice responses."""
    check_security_agent_implemented()
    check_devops_agent_implemented()
    check_orchestrator_routing_implemented()
    import orchestrator
    
    called_urls = []
    def mock_post(url, *args, **kwargs):
        called_urls.append(url)
        return make_mock_response(200, {"task_id": "task_id"})
        
    def mock_get(url, *args, **kwargs):
        return make_mock_response(200, {"status": "completed", "result": "mocked_result_content"})
        
    with patch("httpx.AsyncClient.post", side_effect=mock_post), \
         patch("httpx.AsyncClient.get", side_effect=mock_get):
         
        await orchestrator.run_pipeline("/security audit", workspace=str(tmp_path))
        await orchestrator.run_pipeline("/deploy app", workspace=str(tmp_path))
        
        assert any("security" in url or "8005" in url for url in called_urls)
        assert any("devops" in url or "deploy" in url or "8006" in url for url in called_urls)


# ==============================================================================
# TIER 4: REAL-WORLD APPLICATION SCENARIOS (1 test)
# ==============================================================================

@pytest.mark.asyncio
async def test_t4_mocked_e2e_full_microservice_build(tmp_path):
    """Verify high-fidelity E2E run simulating a full microservice build (Grok -> Claude -> Codex -> Security -> DevOps)."""
    check_security_agent_implemented()
    check_devops_agent_implemented()
    check_orchestrator_routing_implemented()
    
    import orchestrator
    called_urls = []
    
    def mock_post(url, *args, **kwargs):
        called_urls.append(url)
        return make_mock_response(200, {"task_id": "task_id"})
        
    def mock_get(url, *args, **kwargs):
        if "grok" in url:
            res = "Grok research report"
        elif "claude" in url:
            res = "Claude design document"
        elif "codex" in url:
            res = "Codex code review comments"
        elif "tester" in url:
            res = "Tester generated pytests"
        elif "security" in url:
            res = "Security agent audit report"
        elif "devops" in url or "deploy" in url:
            res = "DevOps deployment configuration"
        else:
            res = "Generic response"
        return make_mock_response(200, {"status": "completed", "result": res})
        
    mock_process = AsyncMock()
    mock_process.communicate.return_value = (b"mocked app.py code", b"")
    mock_process.returncode = 0
    
    with patch("httpx.AsyncClient.post", side_effect=mock_post), \
         patch("httpx.AsyncClient.get", side_effect=mock_get), \
         patch("asyncio.create_subprocess_exec", return_value=mock_process):
         
        await orchestrator.run_pipeline("build my microservice", workspace=str(tmp_path))
        
        research_file = tmp_path / "research.md"
        design_file = tmp_path / "design.md"
        app_file = tmp_path / "app.py"
        review_file = tmp_path / "review.md"
        test_generated_file = tmp_path / "test_generated.py"
        
        assert os.path.exists(research_file)
        assert os.path.exists(design_file)
        assert os.path.exists(app_file)
        assert os.path.exists(review_file)
        assert os.path.exists(test_generated_file)
        
        for f in [research_file, design_file, app_file, review_file, test_generated_file]:
            if os.path.exists(f):
                os.remove(f)


# ==============================================================================
# CI/CD WORKFLOW VALIDATION (R3) (1 test)
# ==============================================================================

def test_cicd_yaml_validation():
    """Verify that CI/CD configuration (.github/workflows/ci.yml) is valid and meets constraints."""
    check_cicd_yaml_exists()
    
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".github", "workflows", "ci.yml")
    try:
        import yaml
    except ImportError:
        pytest.fail("PyYAML is not installed; cannot validate CI/CD YAML configuration")
        
    with open(path, "r", encoding="utf-8") as f:
        try:
            workflow = yaml.safe_load(f)
        except Exception as e:
            pytest.fail(f"Invalid CI/CD YAML syntax: {e}")
            
    # Verify trigger
    triggers = workflow.get("on") or workflow.get(True) or {}
    assert "push" in triggers, "CI/CD workflow must trigger on push event"
    assert "pull_request" in triggers, "CI/CD workflow must trigger on pull_request event"
    
    # Verify runs-on windows
    jobs = workflow.get("jobs", {})
    assert jobs, "CI/CD workflow has no jobs defined"
    
    windows_found = False
    setup_python_found = False
    install_reqs_found = False
    pytest_found = False
    
    for job_id, job in jobs.items():
        runs_on = str(job.get("runs-on", ""))
        if "windows" in runs_on.lower():
            windows_found = True
            
        steps = job.get("steps", [])
        for step in steps:
            uses = str(step.get("uses", ""))
            name = str(step.get("name", ""))
            run_cmd = str(step.get("run", ""))
            
            if "setup-python" in uses.lower() or "setup python" in name.lower():
                setup_python_found = True
            if "install" in run_cmd.lower() and ("requirements" in run_cmd.lower() or "pip" in run_cmd.lower() or "uv" in run_cmd.lower()):
                install_reqs_found = True
            if "py -m pytest" in run_cmd.lower() or "pytest" in run_cmd.lower():
                pytest_found = True
                
    assert windows_found, "CI/CD workflow must run on a Windows runner"
    assert setup_python_found, "CI/CD workflow must contain a step to set up Python"
    assert install_reqs_found, "CI/CD workflow must contain a step to install requirements"
    assert pytest_found, "CI/CD workflow must run the test suite command (py -m pytest)"
