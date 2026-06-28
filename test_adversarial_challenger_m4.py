import os
import sys
import pytest
import hashlib
import json
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestrator import call_api, PipelineError
from ag_core.config import MemoryConfig, load_config
from ag_core.memory.vector_store import VectorMemory
import serve

@pytest.mark.asyncio
async def test_call_api_zero_timeout():
    """Verify that call_api with 0 or negative poll_timeout raises PipelineError immediately."""
    async def mock_post(url, **kwargs):
        res = MagicMock()
        res.status_code = 200
        res.content = b'{"task_id": "test-task", "status": "processing"}'
        res.headers = {"X-Payload-SHA256": hashlib.sha256(res.content).hexdigest()}
        res.json.return_value = {"task_id": "test-task", "status": "processing"}
        return res

    with patch("httpx.AsyncClient.post", new=mock_post):
        with pytest.raises(PipelineError) as exc_info:
            await call_api(
                url="http://localhost:8001",
                api_key="mock-key",
                prompt="test prompt",
                poll_timeout=0.0
            )
        assert "timeout" in str(exc_info.value) or "poll_timeout" in str(exc_info.value)

@pytest.mark.asyncio
async def test_call_api_negative_timeout():
    """Verify that call_api with negative poll_timeout raises PipelineError immediately."""
    async def mock_post(url, **kwargs):
        res = MagicMock()
        res.status_code = 200
        res.content = b'{"task_id": "test-task", "status": "processing"}'
        res.headers = {"X-Payload-SHA256": hashlib.sha256(res.content).hexdigest()}
        res.json.return_value = {"task_id": "test-task", "status": "processing"}
        return res

    with patch("httpx.AsyncClient.post", new=mock_post):
        with pytest.raises(PipelineError) as exc_info:
            await call_api(
                url="http://localhost:8001",
                api_key="mock-key",
                prompt="test prompt",
                poll_timeout=-5.0
            )
        assert "timeout" in str(exc_info.value) or "poll_timeout" in str(exc_info.value)

def test_vector_memory_direct_init_respects_genius_memory_db_path():
    """Verify that direct VectorMemory instantiation respects GENIUS_MEMORY_DB_PATH."""
    old_mem = os.environ.get("GENIUS_MEMORY_DB_PATH")
    old_db = os.environ.get("GENIUS_DB_PATH")
    
    try:
        os.environ["GENIUS_MEMORY_DB_PATH"] = "mem_custom_path.db"
        if "GENIUS_DB_PATH" in os.environ:
            del os.environ["GENIUS_DB_PATH"]
            
        vm = VectorMemory(collection_name="test_direct")
        # Direct initialization should now respect GENIUS_MEMORY_DB_PATH
        assert vm.db_path == "mem_custom_path.db"
    finally:
        if old_mem is not None:
            os.environ["GENIUS_MEMORY_DB_PATH"] = old_mem
        elif "GENIUS_MEMORY_DB_PATH" in os.environ:
            del os.environ["GENIUS_MEMORY_DB_PATH"]
            
        if old_db is not None:
            os.environ["GENIUS_DB_PATH"] = old_db
        elif "GENIUS_DB_PATH" in os.environ:
            del os.environ["GENIUS_DB_PATH"]

@pytest.mark.asyncio
async def test_serve_cli_prompt_does_not_block_and_defaults_role():
    """Verify that serve.py main_async defaults to orchestrator and does not block when prompt is provided but roles are not."""
    mock_args = MagicMock()
    mock_args.roles = None
    mock_args.prompt = "my prompt"
    
    with patch("argparse.ArgumentParser.parse_args", return_value=mock_args), \
         patch("serve.interactive_prompt") as mock_interactive, \
         patch("serve.run_pipeline", new_callable=AsyncMock) as mock_run_pipeline, \
         patch("serve.start_server", new_callable=AsyncMock) as mock_start_server:
        
        await serve.main_async()
        mock_interactive.assert_not_called()
        mock_run_pipeline.assert_called_once_with("my prompt")

def test_fastapi_lifespans():
    """Test that all FastAPI apps can start/stop their lifespans cleanly using TestClient."""
    from serve import get_api_app
    
    roles = ["grok", "claude", "codex", "tester", "security", "devops", "dashboard"]
    for role in roles:
        try:
            app = get_api_app(role)
            with TestClient(app) as client:
                # Lifespan should initialize db without errors
                response = client.get("/docs" if role == "dashboard" else "/run")
                # Just making sure we booted and the request didn't raise DB exceptions
                assert response.status_code in [200, 400, 401, 404, 405]
        except Exception as e:
            pytest.fail(f"Lifespan startup failed for role {role}: {e}")
