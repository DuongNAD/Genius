"""Codex reasoning-effort override (GENIUS_CODEX_EFFORT).

Genius-scoped `-c model_reasoning_effort=<v>` on `codex exec` overrides the
user's ~/.codex/config.toml (e.g. a global "ultra") for the pipeline only,
without editing their codex config. Default off -> argv unchanged.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from ag_core.providers.openai_provider import OpenAIProvider

_JSONL = (
    '{"type":"item.completed","item":{"id":"i0",'
    '"type":"agent_message","text":"ok"}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n'
)


def _capture_codex_argv(monkeypatch, env, effort=None):
    monkeypatch.delenv("GENIUS_CODEX_EFFORT", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    captured = {}

    async def run():
        provider = OpenAIProvider()
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = (_JSONL.encode("utf-8"), b"")

        async def fake_exec(*args, **kwargs):
            captured["args"] = list(args)
            return mock_process

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await provider.send_prompt("hi", effort=effort)

    asyncio.run(run())
    return captured["args"]


def test_no_codex_effort_by_default(monkeypatch):
    argv = _capture_codex_argv(monkeypatch, {})
    assert not any(str(a).startswith("model_reasoning_effort=") for a in argv)


def test_codex_effort_high(monkeypatch):
    argv = _capture_codex_argv(monkeypatch, {"GENIUS_CODEX_EFFORT": "high"})
    assert "-c" in argv
    assert "model_reasoning_effort=high" in argv


def test_codex_effort_is_lowercased(monkeypatch):
    argv = _capture_codex_argv(monkeypatch, {"GENIUS_CODEX_EFFORT": "HIGH"})
    assert "model_reasoning_effort=high" in argv


# --- per-request effort arg (@deep threading, no env) ------------------------


def test_codex_per_request_effort_added(monkeypatch):
    argv = _capture_codex_argv(monkeypatch, {}, effort="high")
    assert "model_reasoning_effort=high" in argv


def test_codex_per_request_effort_overrides_env(monkeypatch):
    argv = _capture_codex_argv(
        monkeypatch, {"GENIUS_CODEX_EFFORT": "low"}, effort="high"
    )
    assert "model_reasoning_effort=high" in argv
    assert "model_reasoning_effort=low" not in argv


def test_codex_effort_none_falls_back_to_env(monkeypatch):
    argv = _capture_codex_argv(
        monkeypatch, {"GENIUS_CODEX_EFFORT": "high"}, effort=None
    )
    assert "model_reasoning_effort=high" in argv


def test_codex_argv_unchanged_when_no_effort(monkeypatch):
    argv = _capture_codex_argv(monkeypatch, {}, effort=None)
    assert not any(str(a).startswith("model_reasoning_effort=") for a in argv)
