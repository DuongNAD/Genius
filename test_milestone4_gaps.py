import os
import sys
import pytest
import hashlib
from unittest.mock import AsyncMock, patch, MagicMock
from ag_core.config import MemoryConfig
from orchestrator import call_api, PipelineError, main


def test_db_path_fallback_genius_db_path():
    # Back up environment variables
    old_mem = os.environ.get("GENIUS_MEMORY_DB_PATH")
    old_db = os.environ.get("GENIUS_DB_PATH")

    try:
        if "GENIUS_MEMORY_DB_PATH" in os.environ:
            del os.environ["GENIUS_MEMORY_DB_PATH"]
        os.environ["GENIUS_DB_PATH"] = "my_genius_db.db"
        config = MemoryConfig()
        assert config.db_path == "my_genius_db.db"
    finally:
        # Restore environment variables
        if old_mem is not None:
            os.environ["GENIUS_MEMORY_DB_PATH"] = old_mem
        elif "GENIUS_MEMORY_DB_PATH" in os.environ:
            del os.environ["GENIUS_MEMORY_DB_PATH"]

        if old_db is not None:
            os.environ["GENIUS_DB_PATH"] = old_db
        elif "GENIUS_DB_PATH" in os.environ:
            del os.environ["GENIUS_DB_PATH"]


def test_db_path_fallback_both():
    # Back up environment variables
    old_mem = os.environ.get("GENIUS_MEMORY_DB_PATH")
    old_db = os.environ.get("GENIUS_DB_PATH")

    try:
        os.environ["GENIUS_MEMORY_DB_PATH"] = "mem.db"
        os.environ["GENIUS_DB_PATH"] = "my_genius_db.db"
        config = MemoryConfig()
        assert config.db_path == "mem.db"
    finally:
        # Restore environment variables
        if old_mem is not None:
            os.environ["GENIUS_MEMORY_DB_PATH"] = old_mem
        elif "GENIUS_MEMORY_DB_PATH" in os.environ:
            del os.environ["GENIUS_MEMORY_DB_PATH"]

        if old_db is not None:
            os.environ["GENIUS_DB_PATH"] = old_db
        elif "GENIUS_DB_PATH" in os.environ:
            del os.environ["GENIUS_DB_PATH"]


@pytest.mark.asyncio
async def test_call_api_poll_timeout_exhaustion():
    async def mock_post(*args, **kwargs):
        res = MagicMock()
        res.status_code = 200
        res.content = b'{"task_id": "test-task", "status": "processing"}'
        res.headers = {"X-Payload-SHA256": hashlib.sha256(res.content).hexdigest()}
        res.json.return_value = {"task_id": "test-task", "status": "processing"}
        return res

    async def mock_get(*args, **kwargs):
        res = MagicMock()
        res.status_code = 200
        res.content = b'{"status": "processing"}'
        res.headers = {"X-Payload-SHA256": hashlib.sha256(res.content).hexdigest()}
        res.json.return_value = {"status": "processing"}
        return res

    with patch("httpx.AsyncClient.post", new=mock_post), patch(
        "httpx.AsyncClient.get", new=mock_get
    ), patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        with pytest.raises(PipelineError) as exc_info:
            await call_api(
                url="http://localhost:8001",
                api_key="mock-key",
                prompt="test prompt",
                poll_timeout=0.05,
            )
        assert "timeout" in str(exc_info.value) or "poll_timeout" in str(exc_info.value)
        assert mock_sleep.called


def test_orchestrator_cli_arguments():
    test_args = [
        "orchestrator.py",
        "--prompt",
        "hello",
        "--security-cmd",
        "my-sec",
        "--security-args",
        "arg1",
        "arg2",
        "--devops-cmd",
        "my-dev",
        "--devops-args",
        "arg3",
        "--grok-url",
        "http://grok-override",
        "--claude-url",
        "http://claude-override",
        "--codex-url",
        "http://codex-override",
        "--tester-url",
        "http://tester-override",
        "--security-url",
        "http://security-override",
        "--devops-url",
        "http://devops-override",
        "--api-key",
        "my-api-override",
        "--poll-timeout",
        "120.0",
    ]

    with patch.object(sys, "argv", test_args), patch(
        "orchestrator.run_pipeline", new_callable=AsyncMock
    ) as mock_run:
        try:
            main()
        except SystemExit:
            pass

        mock_run.assert_called_once()
        kwargs = mock_run.call_args[1]
        assert kwargs["prompt"] == "hello"
        assert kwargs["security_cmd"] == "my-sec"
        assert kwargs["security_args"] == ["arg1", "arg2"]
        assert kwargs["devops_cmd"] == "my-dev"
        assert kwargs["devops_args"] == ["arg3"]
        assert kwargs["grok_url"] == "http://grok-override"
        assert kwargs["claude_url"] == "http://claude-override"
        assert kwargs["codex_url"] == "http://codex-override"
        assert kwargs["tester_url"] == "http://tester-override"
        assert kwargs["security_url"] == "http://security-override"
        assert kwargs["devops_url"] == "http://devops-override"
        assert kwargs["api_key_override"] == "my-api-override"
        assert kwargs["poll_timeout"] == 120.0
