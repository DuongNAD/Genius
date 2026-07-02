import asyncio
import json
import sys
import os
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

# Add current workspace to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from serve import (
    normalize_roles,
    main_async,
    wait_for_servers_ready,
    _prune_registry_entry,
    _resolve_registry_path,
    _startup_timeout,
)


def test_resolve_registry_path_empty_env_falls_back_to_default(tmp_path, monkeypatch):
    # Regression: a blank GENIUS_SERVICE_REGISTRY (shipped in .env.example and
    # loaded into os.environ by python-dotenv) used to override the default with
    # "", making os.makedirs(dirname("")) raise FileNotFoundError on Windows and
    # crash every agent server on startup.
    monkeypatch.setenv("GENIUS_SERVICE_REGISTRY", "")
    # Should not raise, and must fall back to the in-repo default path.
    path = _resolve_registry_path()
    assert path.endswith(os.path.join(".agents", "service_registry.json"))


def test_resolve_registry_path_honours_explicit_env(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "dir" / "registry.json"
    monkeypatch.setenv("GENIUS_SERVICE_REGISTRY", str(target))
    path = _resolve_registry_path()
    assert path == str(target)
    # Parent directory must have been created.
    assert target.parent.is_dir()


def test_normalize_roles():
    assert normalize_roles("researcher,claude") == ["researcher", "claude"]
    # Legacy "grok"-flavoured tokens map to the renamed researcher role.
    assert normalize_roles("grok,claude") == ["researcher", "claude"]
    assert normalize_roles("grok-api,4") == ["researcher", "tester"]
    assert normalize_roles("grok, 2, 3, 5") == [
        "researcher",
        "claude",
        "codex",
        "orchestrator",
    ]


@pytest.mark.asyncio
@patch("serve.start_server", new_callable=AsyncMock)
@patch("serve.run_pipeline", new_callable=AsyncMock)
@patch("argparse.ArgumentParser.parse_args")
async def test_serve_cli_roles_only(
    mock_parse_args, mock_run_pipeline, mock_start_server
):
    # Simulate '--roles grok,claude' (legacy researcher alias on the CLI)
    mock_args = MagicMock()
    mock_args.roles = "grok,claude"
    mock_args.prompt = None
    mock_parse_args.return_value = mock_args

    await main_async()

    assert mock_start_server.call_count == 2
    mock_start_server.assert_any_call("researcher", 8001)
    mock_start_server.assert_any_call("claude", 8002)
    assert not mock_run_pipeline.called


@pytest.mark.asyncio
@patch("serve.start_server", new_callable=AsyncMock)
@patch("serve.run_pipeline", new_callable=AsyncMock)
@patch("argparse.ArgumentParser.parse_args")
async def test_serve_cli_orchestrator(
    mock_parse_args, mock_run_pipeline, mock_start_server
):
    # Simulate '--roles orchestrator --prompt "Build a calculator"'
    mock_args = MagicMock()
    mock_args.roles = "orchestrator"
    mock_args.prompt = "Build a calculator"
    mock_parse_args.return_value = mock_args

    await main_async()

    assert not mock_start_server.called
    mock_run_pipeline.assert_called_once_with("Build a calculator")


@pytest.mark.asyncio
@patch("serve.start_server", new_callable=AsyncMock)
@patch("serve.interactive_prompt")
@patch("argparse.ArgumentParser.parse_args")
async def test_serve_interactive_fallback(
    mock_parse_args, mock_interactive_prompt, mock_start_server
):
    # Simulate no args passed, interactive prompt selects 'codex'
    mock_args = MagicMock()
    mock_args.roles = None
    mock_args.prompt = None
    mock_parse_args.return_value = mock_args

    mock_interactive_prompt.return_value = ["codex"]

    await main_async()

    assert mock_interactive_prompt.called
    mock_start_server.assert_called_once_with("codex", 8003)


@pytest.mark.asyncio
@patch("serve.start_server", new_callable=AsyncMock)
@patch("serve.run_pipeline", new_callable=AsyncMock)
@patch("argparse.ArgumentParser.parse_args")
async def test_serve_cli_interactive(
    mock_parse_args, mock_run_pipeline, mock_start_server
):
    # Simulate '--roles orchestrator --prompt "Build a calculator" --interactive'
    mock_args = MagicMock()
    mock_args.roles = "orchestrator"
    mock_args.prompt = "Build a calculator"
    mock_args.interactive = True
    mock_args.auto_pilot = False
    mock_parse_args.return_value = mock_args

    await main_async()

    assert not mock_start_server.called
    mock_run_pipeline.assert_called_once_with("Build a calculator", interactive=True)


@pytest.mark.asyncio
@patch("serve.start_server", new_callable=AsyncMock)
@patch("serve.run_pipeline", new_callable=AsyncMock)
@patch("argparse.ArgumentParser.parse_args")
async def test_serve_cli_auto_pilot(
    mock_parse_args, mock_run_pipeline, mock_start_server
):
    # Simulate '--auto-pilot --prompt "Build a calculator"'
    mock_args = MagicMock()
    mock_args.roles = None
    mock_args.prompt = "Build a calculator"
    mock_args.interactive = False
    mock_args.auto_pilot = True
    mock_parse_args.return_value = mock_args

    await main_async()

    # All 7 microservice servers should have been started
    assert mock_start_server.call_count == 7
    mock_start_server.assert_any_call("researcher", 8001)
    mock_start_server.assert_any_call("claude", 8002)
    mock_start_server.assert_any_call("codex", 8003)
    mock_start_server.assert_any_call("tester", 8004)
    mock_start_server.assert_any_call("security", 8005)
    mock_start_server.assert_any_call("devops", 8006)
    mock_start_server.assert_any_call("dashboard", 8080)

    # run_pipeline should be called with interactive=False
    mock_run_pipeline.assert_called_once_with("Build a calculator", interactive=False)


@pytest.mark.asyncio
@patch("serve.start_server", new_callable=AsyncMock)
@patch("serve.run_pipeline", new_callable=AsyncMock)
@patch("argparse.ArgumentParser.parse_args")
async def test_serve_auto_pilot_exits_nonzero_on_pipeline_failure(
    mock_parse_args, mock_run_pipeline, mock_start_server
):
    # Regression (F3): auto-pilot used to swallow pipeline errors and exit 0.
    mock_args = MagicMock()
    mock_args.roles = None
    mock_args.prompt = "Build a calculator"
    mock_args.interactive = False
    mock_args.auto_pilot = True
    mock_parse_args.return_value = mock_args

    mock_run_pipeline.side_effect = RuntimeError("pipeline exploded")

    with pytest.raises(SystemExit) as exc_info:
        await main_async()
    assert exc_info.value.code == 1


# --- Skill server /health + startup readiness poll -------------------------


def test_skill_app_health_endpoint_is_unauthenticated():
    from fastapi.testclient import TestClient
    from ag_core.skill_app import create_skill_app

    # Built with the legacy "grok" role id: the alias still boots the
    # researcher app, and /health reports the CANONICAL role id.
    client = TestClient(create_skill_app("grok"))
    # No JWT / checksum headers at all: /health must still answer.
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "role": "researcher"}


@pytest.mark.asyncio
async def test_wait_for_servers_ready_success(monkeypatch):
    async def fake_get(self, url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        return resp

    monkeypatch.setattr("httpx.AsyncClient.get", fake_get)
    # Must return without raising once every role answered 200.
    await wait_for_servers_ready({"grok": 8001, "claude": 8002}, timeout=5.0)


@pytest.mark.asyncio
async def test_wait_for_servers_ready_deadline_lists_missing_roles(monkeypatch):
    import httpx

    async def fake_get(self, url, **kwargs):
        raise httpx.ConnectError("nobody home")

    monkeypatch.setattr("httpx.AsyncClient.get", fake_get)
    with pytest.raises(RuntimeError) as exc_info:
        await wait_for_servers_ready({"grok": 8001}, timeout=0.3)
    assert "grok" in str(exc_info.value)
    assert "ready" in str(exc_info.value)


@pytest.mark.asyncio
async def test_wait_for_servers_ready_aborts_on_crashed_server_task(monkeypatch):
    import httpx

    async def fake_get(self, url, **kwargs):
        raise httpx.ConnectError("nobody home")

    monkeypatch.setattr("httpx.AsyncClient.get", fake_get)

    async def boom():
        raise RuntimeError("server crashed at startup")

    task = asyncio.get_running_loop().create_task(boom())
    await asyncio.sleep(0)  # let the task run (and fail)
    with pytest.raises(RuntimeError) as exc_info:
        await wait_for_servers_ready({"grok": 8001}, server_tasks=[task], timeout=5.0)
    assert "crashed" in str(exc_info.value)


def test_startup_timeout_env_override(monkeypatch):
    monkeypatch.setenv("GENIUS_STARTUP_TIMEOUT", "12.5")
    assert _startup_timeout() == 12.5
    monkeypatch.setenv("GENIUS_STARTUP_TIMEOUT", "not-a-number")
    assert _startup_timeout() == 30.0
    monkeypatch.setenv("GENIUS_STARTUP_TIMEOUT", "-3")
    assert _startup_timeout() == 30.0
    monkeypatch.delenv("GENIUS_STARTUP_TIMEOUT", raising=False)
    assert _startup_timeout() == 30.0


# --- Service registry hygiene ----------------------------------------------


def test_prune_registry_entry_removes_own_port(tmp_path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps({"grok": 9001, "claude": 8002}), encoding="utf-8"
    )
    _prune_registry_entry(str(registry_path), "grok", 9001)
    remaining = json.loads(registry_path.read_text(encoding="utf-8"))
    assert remaining == {"claude": 8002}


def test_prune_registry_entry_keeps_entry_of_newer_instance(tmp_path):
    # A newer instance re-registered 'grok' on another port: do not prune it.
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({"grok": 9002}), encoding="utf-8")
    _prune_registry_entry(str(registry_path), "grok", 9001)
    remaining = json.loads(registry_path.read_text(encoding="utf-8"))
    assert remaining == {"grok": 9002}


def test_prune_registry_entry_missing_file_is_noop(tmp_path):
    # Must not raise when the registry never got written.
    _prune_registry_entry(str(tmp_path / "nope.json"), "grok", 9001)


def test_load_config_reads_registry_with_blank_env(monkeypatch, tmp_path):
    """Regression (F4): a blank GENIUS_SERVICE_REGISTRY (shipped in .env.example)
    used to silently disable the registry override on the READ side, killing
    dynamic-port discovery. Blank must fall back to the in-repo default path."""
    from ag_core.config import load_config

    default_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".agents", "service_registry.json"
    )
    os.makedirs(os.path.dirname(default_path), exist_ok=True)
    original = None
    if os.path.exists(default_path):
        with open(default_path, "r", encoding="utf-8") as f:
            original = f.read()
    try:
        # Legacy registry key "grok" (pre-rename) must still map onto the
        # renamed services.researcher field.
        with open(default_path, "w", encoding="utf-8") as f:
            json.dump({"grok": 9166}, f)
        monkeypatch.setenv("GENIUS_SERVICE_REGISTRY", "")
        config = load_config()
        # Under pytest, URLs get a /role suffix (the raw registry key).
        assert config.services.researcher == "http://localhost:9166/grok"
    finally:
        if original is None:
            os.remove(default_path)
        else:
            with open(default_path, "w", encoding="utf-8") as f:
                f.write(original)


def test_load_config_honours_explicit_registry_env(monkeypatch, tmp_path):
    from ag_core.config import load_config

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({"claude": 9177}), encoding="utf-8")
    monkeypatch.setenv("GENIUS_SERVICE_REGISTRY", str(registry_path))
    config = load_config()
    assert config.services.claude_architect == "http://localhost:9177/claude"
