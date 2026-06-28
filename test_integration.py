import os
import sys
import runpy
import asyncio
import httpx
import pytest
import hashlib
from unittest.mock import AsyncMock, patch
from ag_core.config import load_config
from ag_core.agents.grok_researcher import GrokResearcherAgent
from ag_core.agents.claude_architect import ClaudeArchitectAgent
from ag_core.agents.codex_reviewer import CodexReviewerAgent
from ag_core.agents.tester import TesterAgent
from ag_core.providers.grok_provider import GrokProvider
from ag_core.providers.anthropic_provider import AnthropicProvider
from ag_core.providers.openai_provider import OpenAIProvider

@pytest.fixture(autouse=True)
def mock_subprocess(request):
    """Automatically mock asyncio.create_subprocess_exec to prevent executing real CLI."""
    import json
    test_name = request.node.name
    
    mock_process = AsyncMock()
    mock_process.returncode = 0
    
    content = "Mock LLM Content"
    input_tokens = 10
    output_tokens = 20
    
    if "test_grok_researcher_agent_flow" in test_name:
        content = "Research Report Content"
    elif "test_claude_architect_agent_flow" in test_name:
        content = "Architecture Design Document"
    elif "test_codex_reviewer_agent_flow" in test_name:
        content = "Code Review Summary"
    elif "test_tester_agent_flow" in test_name:
        content = "def test_dummy(): pass"
    elif "test_skill_bootstrap_grok_researcher" in test_name:
        content = "Skill Bootstrap Grok Researcher Output"
    elif "test_skill_bootstrap_claude_architect" in test_name:
        content = "Skill Bootstrap Claude Architect Output"
    elif "test_skill_bootstrap_codex_reviewer" in test_name:
        content = "Skill Bootstrap Codex Reviewer Output"
    elif "test_skill_bootstrap_tester_agent" in test_name:
        content = "def test_bootstrap(): pass"
    elif "test_grok_api_server" in test_name:
        content = "API Grok Research Content"
    elif "test_claude_api_server" in test_name:
        content = "API Claude Design Content"
    elif "test_codex_api_server" in test_name:
        content = "API Codex Review Content"
    elif "test_tester_api_server" in test_name:
        content = "def test_api(): pass"
        
    if "codex" in test_name or "tester" in test_name:
        line1 = json.dumps({"event": "agent_message", "item": {"text": content}})
        line2 = json.dumps({"event": "turn.completed", "turn.completed": {"usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}}})
        jsonl_output = f"{line1}\n{line2}\n"
        mock_process.communicate.return_value = (
            jsonl_output.encode("utf-8"),
            b""
        )
    else:
        res_json = {
            "result": content,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens
            }
        }
        mock_process.communicate.return_value = (
            json.dumps(res_json).encode("utf-8"),
            b""
        )
    
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process
        yield mock_exec

def test_config_loading_and_merging():
    config = load_config()
    assert config.app.name == "Antigravity Core"
    assert config.app.version == "2.0"
    assert config.models.openai == "gpt-4o"
    assert config.models.anthropic == "claude-3-5-sonnet"
    assert config.models.grok == "grok-2"
    assert config.scanner.chunk_size_limit == 8000
    assert ".git/" in config.scanner.exclude_patterns
    
    # Secrets should be merged from .env or taken from outer environment (since override=False)
    expected_openai = os.environ.get("OPENAI_API_KEY", "mock-openai-key")
    expected_anthropic = os.environ.get("ANTHROPIC_API_KEY", "mock-anthropic-key")
    expected_grok = os.environ.get("GROK_API_KEY", "mock-grok-key")
    assert config.openai_api_key == expected_openai
    assert config.anthropic_api_key == expected_anthropic
    assert config.grok_api_key == expected_grok

def test_grok_researcher_agent_flow(tmp_path, monkeypatch):
    async def run_test():
        monkeypatch.chdir(tmp_path)
        
        # Create a dummy file to scan
        (tmp_path / "dummy.txt").write_text("Hello Grok", encoding="utf-8")
        
        provider = GrokProvider(api_key="mock-key", model_name="grok-2")
        
        mock_res = httpx.Response(
            status_code=200,
            json={
                "choices": [{"message": {"content": "Research Report Content"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30
                }
            },
            request=httpx.Request("POST", "https://api.x.ai/v1/chat/completions")
        )
        
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_res
            
            config = load_config()
            agent = GrokResearcherAgent(provider=provider, config=config)
            result = await agent.run(prompt="Analyze requirements.")
            
            assert result == "Research Report Content"
            assert os.path.exists("research.md")
            with open("research.md", "r", encoding="utf-8") as f:
                assert f.read() == "Research Report Content"
                
    asyncio.run(run_test())

def test_claude_architect_agent_flow(tmp_path, monkeypatch):
    async def run_test():
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dummy.txt").write_text("Hello Claude", encoding="utf-8")
        
        provider = AnthropicProvider(api_key="mock-key", model_name="claude-3-5-sonnet")
        
        mock_res = httpx.Response(
            status_code=200,
            json={
                "content": [{"text": "Architecture Design Document"}],
                "usage": {
                    "input_tokens": 15,
                    "output_tokens": 25
                }
            },
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_res
            
            config = load_config()
            agent = ClaudeArchitectAgent(provider=provider, config=config)
            result = await agent.run(prompt="Create architecture.")
            
            assert result == "Architecture Design Document"
            assert os.path.exists("design.md")
            with open("design.md", "r", encoding="utf-8") as f:
                assert f.read() == "Architecture Design Document"
                
    asyncio.run(run_test())

def test_codex_reviewer_agent_flow(tmp_path, monkeypatch):
    async def run_test():
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dummy.txt").write_text("Hello Codex", encoding="utf-8")
        
        provider = OpenAIProvider(api_key="mock-key", model_name="gpt-4o")
        
        mock_res = httpx.Response(
            status_code=200,
            json={
                "choices": [{"message": {"content": "Code Review Summary"}}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 18,
                    "total_tokens": 30
                }
            },
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        )
        
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_res
            
            config = load_config()
            agent = CodexReviewerAgent(provider=provider, config=config)
            result = await agent.run(prompt="Review code.")
            
            assert result.startswith("Code Review Summary")
            assert os.path.exists("review.md")
            with open("review.md", "r", encoding="utf-8") as f:
                assert f.read().startswith("Code Review Summary")
                
    asyncio.run(run_test())

def test_tester_agent_flow(tmp_path, monkeypatch):
    async def run_test():
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dummy.txt").write_text("Hello Tester", encoding="utf-8")
        
        provider = OpenAIProvider(api_key="mock-key", model_name="gpt-4o")
        
        mock_res = httpx.Response(
            status_code=200,
            json={
                "choices": [{"message": {"content": "def test_dummy(): pass"}}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 18,
                    "total_tokens": 30
                }
            },
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        )
        
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_res
            
            config = load_config()
            agent = TesterAgent(provider=provider, config=config)
            result = await agent.run(prompt="Generate tests.")
            
            assert result.startswith("def test_dummy(): pass")
            assert os.path.exists("test_generated.py")
            with open("test_generated.py", "r", encoding="utf-8") as f:
                assert f.read() == "def test_dummy(): pass"
                
    asyncio.run(run_test())

def test_skill_bootstrap_grok_researcher(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "app:\n  name: \"Antigravity Core\"\n  version: \"2.0\"\nmodels:\n  openai: \"gpt-4o\"\n  anthropic: \"claude-3-5-sonnet\"\n  grok: \"grok-2\"\nscanner:\n  chunk_size_limit: 8000\n  exclude_patterns: [\".git/\"]\n",
        encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=mock-openai-key\nANTHROPIC_API_KEY=mock-anthropic-key\nGROK_API_KEY=mock-grok-key\n",
        encoding="utf-8"
    )
    
    mock_res = httpx.Response(
        status_code=200,
        json={
            "choices": [{"message": {"content": "Skill Bootstrap Grok Researcher Output"}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30
            }
        },
        request=httpx.Request("POST", "https://api.x.ai/v1/chat/completions")
    )
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
         patch("sys.exit") as mock_exit:
        mock_post.return_value = mock_res
        
        run_py_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            ".agents", "skills", "grok_researcher", "run.py"
        )
        
        runpy.run_path(run_py_path, run_name="__main__")
        
        assert not mock_exit.called
        assert os.path.exists("research.md")
        with open("research.md", "r", encoding="utf-8") as f:
            assert f.read() == "Skill Bootstrap Grok Researcher Output"

def test_skill_bootstrap_claude_architect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "app:\n  name: \"Antigravity Core\"\n  version: \"2.0\"\nmodels:\n  openai: \"gpt-4o\"\n  anthropic: \"claude-3-5-sonnet\"\n  grok: \"grok-2\"\nscanner:\n  chunk_size_limit: 8000\n  exclude_patterns: [\".git/\"]\n",
        encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=mock-openai-key\nANTHROPIC_API_KEY=mock-anthropic-key\nGROK_API_KEY=mock-grok-key\n",
        encoding="utf-8"
    )
    
    mock_res = httpx.Response(
        status_code=200,
        json={
            "content": [{"text": "Skill Bootstrap Claude Architect Output"}],
            "usage": {
                "input_tokens": 15,
                "output_tokens": 25
            }
        },
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
         patch("sys.exit") as mock_exit:
        mock_post.return_value = mock_res
        
        run_py_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            ".agents", "skills", "claude_architect", "run.py"
        )
        
        runpy.run_path(run_py_path, run_name="__main__")
        
        assert not mock_exit.called
        assert os.path.exists("design.md")
        with open("design.md", "r", encoding="utf-8") as f:
            assert f.read() == "Skill Bootstrap Claude Architect Output"

def test_skill_bootstrap_codex_reviewer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "app:\n  name: \"Antigravity Core\"\n  version: \"2.0\"\nmodels:\n  openai: \"gpt-4o\"\n  anthropic: \"claude-3-5-sonnet\"\n  grok: \"grok-2\"\nscanner:\n  chunk_size_limit: 8000\n  exclude_patterns: [\".git/\"]\n",
        encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=mock-openai-key\nANTHROPIC_API_KEY=mock-anthropic-key\nGROK_API_KEY=mock-grok-key\n",
        encoding="utf-8"
    )
    
    mock_res = httpx.Response(
        status_code=200,
        json={
            "choices": [{"message": {"content": "Skill Bootstrap Codex Reviewer Output"}}],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 18,
                "total_tokens": 30
            }
        },
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    )
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
         patch("sys.exit") as mock_exit:
        mock_post.return_value = mock_res
        
        run_py_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            ".agents", "skills", "codex_reviewer", "run.py"
        )
        
        runpy.run_path(run_py_path, run_name="__main__")
        
        assert not mock_exit.called
        assert os.path.exists("review.md")
        with open("review.md", "r", encoding="utf-8") as f:
            assert f.read().startswith("Skill Bootstrap Codex Reviewer Output")

def test_skill_bootstrap_tester_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "app:\n  name: \"Antigravity Core\"\n  version: \"2.0\"\nmodels:\n  openai: \"gpt-4o\"\n  anthropic: \"claude-3-5-sonnet\"\n  grok: \"grok-2\"\nscanner:\n  chunk_size_limit: 8000\n  exclude_patterns: [\".git/\"]\n",
        encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=mock-openai-key\nANTHROPIC_API_KEY=mock-anthropic-key\nGROK_API_KEY=mock-grok-key\n",
        encoding="utf-8"
    )
    
    mock_res = httpx.Response(
        status_code=200,
        json={
            "choices": [{"message": {"content": "def test_bootstrap(): pass"}}],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 18,
                "total_tokens": 30
            }
        },
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    )
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
         patch("sys.exit") as mock_exit:
        mock_post.return_value = mock_res
        
        run_py_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            ".agents", "skills", "tester_agent", "run.py"
        )
        
        runpy.run_path(run_py_path, run_name="__main__")
        
        assert not mock_exit.called
        assert os.path.exists("test_generated.py")
        with open("test_generated.py", "r", encoding="utf-8") as f:
            assert f.read() == "def test_bootstrap(): pass"

import importlib.util

def load_api_app(role: str):
    root_dir = os.path.dirname(os.path.abspath(__file__))
    if role == "grok":
        path = os.path.join(root_dir, ".agents", "skills", "grok_researcher", "api.py")
    elif role == "claude":
        path = os.path.join(root_dir, ".agents", "skills", "claude_architect", "api.py")
    elif role == "codex":
        path = os.path.join(root_dir, ".agents", "skills", "codex_reviewer", "api.py")
    elif role == "tester":
        path = os.path.join(root_dir, ".agents", "skills", "tester_agent", "api.py")
    else:
        raise ValueError(f"Unknown role: {role}")
        
    spec = importlib.util.spec_from_file_location(f"{role}_api_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app

def make_test_headers(api_key: str, body_bytes: bytes) -> dict:
    import hashlib
    import time
    from ag_core.utils.jwt import encode_jwt
    
    if api_key.count('.') == 2:
        token = api_key
    else:
        payload = {
            "sub": "test",
            "exp": time.time() + 300
        }
        token = encode_jwt(payload, api_key)
        
    checksum = hashlib.sha256(body_bytes).hexdigest()
    return {
        "X-API-Key": token,
        "X-Payload-SHA256": checksum,
        "Content-Type": "application/json"
    }

def test_grok_api_server():
    grok_app = load_api_app("grok")
    from fastapi.testclient import TestClient
    client = TestClient(grok_app)
    
    import json
    payload = {"prompt": "Analyze requirements"}
    body_bytes = json.dumps(payload).encode("utf-8")
    headers_invalid = make_test_headers("wrong", body_bytes)
    
    # Test invalid key
    response = client.post("/run", content=body_bytes, headers=headers_invalid)
    assert response.status_code == 401
    
    # Test valid key with mock HTTP response
    mock_res = httpx.Response(
        status_code=200,
        json={
            "choices": [{"message": {"content": "API Grok Research Content"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        },
        request=httpx.Request("POST", "https://api.x.ai/v1/chat/completions")
    )
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_res
        
        headers_valid = make_test_headers("mock-skill-key", body_bytes)
        response = client.post("/run", content=body_bytes, headers=headers_valid)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "processing"
        task_id = data["task_id"]
        assert task_id is not None
        
        get_headers = make_test_headers("mock-skill-key", b"")
        status_response = client.get(f"/status/{task_id}", headers=get_headers)
        assert status_response.status_code == 200
        status_data = status_response.json()
        assert status_data["status"] == "completed"
        assert status_data["result"] == "API Grok Research Content"

def test_claude_api_server():
    claude_app = load_api_app("claude")
    from fastapi.testclient import TestClient
    client = TestClient(claude_app)
    
    import json
    payload = {"prompt": "Create design"}
    body_bytes = json.dumps(payload).encode("utf-8")
    headers_invalid = make_test_headers("wrong", body_bytes)
    
    response = client.post("/run", content=body_bytes, headers=headers_invalid)
    assert response.status_code == 401
    
    mock_res = httpx.Response(
        status_code=200,
        json={
            "content": [{"text": "API Claude Design Content"}],
            "usage": {"input_tokens": 15, "output_tokens": 25}
        },
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_res
        
        headers_valid = make_test_headers("mock-skill-key", body_bytes)
        response = client.post("/run", content=body_bytes, headers=headers_valid)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "processing"
        task_id = data["task_id"]
        
        get_headers = make_test_headers("mock-skill-key", b"")
        status_response = client.get(f"/status/{task_id}", headers=get_headers)
        assert status_response.status_code == 200
        status_data = status_response.json()
        assert status_data["status"] == "completed"
        assert status_data["result"] == "API Claude Design Content"
def test_codex_api_server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dummy.py").write_text("def run(): pass", encoding="utf-8")
    codex_app = load_api_app("codex")
    from fastapi.testclient import TestClient
    client = TestClient(codex_app)
    
    import json
    payload = {"prompt": "Review code"}
    body_bytes = json.dumps(payload).encode("utf-8")
    headers_invalid = make_test_headers("wrong", body_bytes)
    
    response = client.post("/run", content=body_bytes, headers=headers_invalid)
    assert response.status_code == 401
    
    mock_res = httpx.Response(
        status_code=200,
        json={
            "choices": [{"message": {"content": "API Codex Review Content"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 18, "total_tokens": 30}
        },
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    )
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_res
        
        headers_valid = make_test_headers("mock-skill-key", body_bytes)
        response = client.post("/run", content=body_bytes, headers=headers_valid)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "processing"
        task_id = data["task_id"]
        
        get_headers = make_test_headers("mock-skill-key", b"")
        status_response = client.get(f"/status/{task_id}", headers=get_headers)
        assert status_response.status_code == 200
        status_data = status_response.json()
        assert status_data["status"] == "completed"
        assert status_data["result"].startswith("API Codex Review Content")
def test_tester_api_server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dummy.py").write_text("def run(): pass", encoding="utf-8")
    tester_app = load_api_app("tester")
    from fastapi.testclient import TestClient
    client = TestClient(tester_app)
    
    import json
    payload = {"prompt": "Generate tests"}
    body_bytes = json.dumps(payload).encode("utf-8")
    headers_invalid = make_test_headers("wrong", body_bytes)
    
    response = client.post("/run", content=body_bytes, headers=headers_invalid)
    assert response.status_code == 401
    
    mock_res = httpx.Response(
        status_code=200,
        json={
            "choices": [{"message": {"content": "def test_api(): pass"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 18, "total_tokens": 30}
        },
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    )
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_res
        
        headers_valid = make_test_headers("mock-skill-key", body_bytes)
        response = client.post("/run", content=body_bytes, headers=headers_valid)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "processing"
        task_id = data["task_id"]
        
        get_headers = make_test_headers("mock-skill-key", b"")
        status_response = client.get(f"/status/{task_id}", headers=get_headers)
        assert status_response.status_code == 200
        status_data = status_response.json()
        assert status_data["status"] == "completed"
        assert status_data["result"].startswith("def test_api(): pass")

def test_api_checksum_mismatch_request():
    grok_app = load_api_app("grok")
    from fastapi.testclient import TestClient
    client = TestClient(grok_app)
    
    import time
    from ag_core.utils.jwt import encode_jwt
    token = encode_jwt({"sub": "test", "exp": time.time() + 300}, "mock-skill-key")
    
    # 1. Missing header
    response = client.post("/run", json={"prompt": "test"}, headers={"X-API-Key": token})
    assert response.status_code == 400
    assert "Missing X-Payload-SHA256" in response.json()["detail"]
    
    # 2. Incorrect checksum
    headers = {
        "X-API-Key": token,
        "X-Payload-SHA256": "wrongchecksum"
    }
    response = client.post("/run", json={"prompt": "test"}, headers=headers)
    assert response.status_code == 400
    assert "Checksum mismatch" in response.json()["detail"]

@pytest.mark.asyncio
async def test_orchestrator_checksum_mismatch_response_retries():
    from orchestrator import call_api, PipelineError
    
    # Mismatch response payload checksum
    mock_res = httpx.Response(
        status_code=200,
        json={"status": "processing", "task_id": "test-task"},
        headers={"X-Payload-SHA256": "mismatched_checksum_value"},
        request=httpx.Request("POST", "http://localhost:8001/run")
    )
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_post.return_value = mock_res
        
        with pytest.raises(PipelineError) as exc_info:
            await call_api("http://localhost:8001", "mock-skill-key", "test prompt")
            
        assert mock_post.call_count == 3
        assert "Response checksum mismatch" in str(exc_info.value)
        assert mock_sleep.called
