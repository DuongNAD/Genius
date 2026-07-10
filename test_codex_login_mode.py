"""Codex login mode: strip proxy OPENAI_* vars from the codex subprocess.

A machine may run a LiteLLM / OpenAI-compatible proxy exposed via
OPENAI_API_KEY + OPENAI_BASE_URL. Codex is a login-based CLI, so in login
mode (the default) Genius must hide those two vars from the codex
subprocess ONLY — codex then uses its ChatGPT login, while the proxy env
stays intact process-wide for other tools.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from ag_core.providers.openai_provider import (
    OpenAIProvider,
    _codex_subprocess_env,
)

_PROXY = {
    "OPENAI_API_KEY": "sk-litellm-proxy-key",
    "OPENAI_BASE_URL": "http://localhost:4000/v1",
}


def test_login_mode_default_strips_proxy_vars(monkeypatch):
    monkeypatch.delenv("GENIUS_CODEX_LOGIN_MODE", raising=False)
    for k, v in _PROXY.items():
        monkeypatch.setenv(k, v)
    env = _codex_subprocess_env()
    assert env is not None
    assert "OPENAI_API_KEY" not in env
    assert "OPENAI_BASE_URL" not in env
    # It is a full copy of the environment minus the two proxy vars.
    assert len(env) > 1


def test_optout_inherits_proxy_vars(monkeypatch):
    for k, v in _PROXY.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("GENIUS_CODEX_LOGIN_MODE", "0")
    assert _codex_subprocess_env() is None  # inherit parent env unchanged


def test_no_proxy_vars_inherits_unchanged(monkeypatch):
    monkeypatch.delenv("GENIUS_CODEX_LOGIN_MODE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    # Nothing to hide -> None (inherit), so we don't pay for a dict copy.
    assert _codex_subprocess_env() is None


def test_send_prompt_passes_stripped_env_to_codex(monkeypatch):
    monkeypatch.delenv("GENIUS_CODEX_LOGIN_MODE", raising=False)
    for k, v in _PROXY.items():
        monkeypatch.setenv(k, v)

    async def run():
        provider = OpenAIProvider()
        mock_process = AsyncMock()
        mock_process.returncode = 0
        jsonl = (
            '{"type":"thread.started","thread_id":"t1"}\n'
            '{"type":"item.completed","item":{"id":"i0",'
            '"type":"agent_message","text":"ok"}}\n'
            '{"type":"turn.completed","usage":'
            '{"input_tokens":1,"output_tokens":1}}\n'
        )
        mock_process.communicate.return_value = (jsonl.encode("utf-8"), b"")
        with patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = mock_process
            await provider.send_prompt("hi")
        env = mock_exec.call_args.kwargs.get("env")
        assert env is not None
        assert "OPENAI_API_KEY" not in env
        assert "OPENAI_BASE_URL" not in env

    asyncio.run(run())


def test_send_prompt_optout_passes_none_env(monkeypatch):
    for k, v in _PROXY.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("GENIUS_CODEX_LOGIN_MODE", "0")

    async def run():
        provider = OpenAIProvider()
        mock_process = AsyncMock()
        mock_process.returncode = 0
        jsonl = (
            '{"type":"item.completed","item":{"id":"i0",'
            '"type":"agent_message","text":"ok"}}\n'
            '{"type":"turn.completed","usage":'
            '{"input_tokens":1,"output_tokens":1}}\n'
        )
        mock_process.communicate.return_value = (jsonl.encode("utf-8"), b"")
        with patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = mock_process
            await provider.send_prompt("hi")
        # env=None => codex inherits the parent env (proxy vars intact).
        assert mock_exec.call_args.kwargs.get("env") is None

    asyncio.run(run())
