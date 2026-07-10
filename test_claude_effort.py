"""Claude reasoning-effort + model-fallback flags (R5 follow-up).

The plan/architect stage runs on the claude backend; GENIUS_CLAUDE_EFFORT
maps to the Claude Code `--effort` flag (low|medium|high|xhigh|max — there
is NO "ultra" tier), and GENIUS_CLAUDE_FALLBACK_MODEL maps to
`--fallback-model` (e.g. Opus 4.8 primary, Fable 5 fallback). Both default
off so the claude argv is unchanged unless set.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from ag_core.providers.anthropic_provider import AnthropicProvider


def _capture_claude_argv(monkeypatch, env, role=None):
    for k in (
        "GENIUS_CLAUDE_EFFORT",
        "GENIUS_CLAUDE_FALLBACK_MODEL",
        "GENIUS_CLAUDE_EFFORT_TESTER",
        "GENIUS_CLAUDE_EFFORT_CLAUDE",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    captured = {}

    async def run():
        kwargs = {"model_name": "claude-opus-4-8"}
        if role:
            kwargs["role"] = role
        provider = AnthropicProvider(**kwargs)
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (
            json.dumps({"result": "ok", "usage": {}}).encode("utf-8"),
            b"",
        )

        async def fake_exec(*args, **kwargs):
            captured["args"] = list(args)
            return mock_process

        with (
            patch("shutil.which", return_value="/usr/local/bin/claude"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            await provider.send_prompt("hi")

    asyncio.run(run())
    return captured["args"]


def test_no_effort_or_fallback_by_default(monkeypatch):
    argv = _capture_claude_argv(monkeypatch, {})
    assert "--effort" not in argv
    assert "--fallback-model" not in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"


def test_effort_max_passed(monkeypatch):
    argv = _capture_claude_argv(monkeypatch, {"GENIUS_CLAUDE_EFFORT": "max"})
    assert argv[argv.index("--effort") + 1] == "max"


def test_effort_is_lowercased(monkeypatch):
    argv = _capture_claude_argv(monkeypatch, {"GENIUS_CLAUDE_EFFORT": "XHIGH"})
    assert argv[argv.index("--effort") + 1] == "xhigh"


def test_invalid_effort_ultra_is_ignored(monkeypatch):
    # "ultra" is a codex concept, not a Claude tier -> dropped, not passed.
    argv = _capture_claude_argv(monkeypatch, {"GENIUS_CLAUDE_EFFORT": "ultra"})
    assert "--effort" not in argv


def test_fallback_model_passed(monkeypatch):
    argv = _capture_claude_argv(
        monkeypatch, {"GENIUS_CLAUDE_FALLBACK_MODEL": "claude-fable-5"}
    )
    assert argv[argv.index("--fallback-model") + 1] == "claude-fable-5"


def test_effort_and_fallback_together(monkeypatch):
    argv = _capture_claude_argv(
        monkeypatch,
        {
            "GENIUS_CLAUDE_EFFORT": "max",
            "GENIUS_CLAUDE_FALLBACK_MODEL": "claude-fable-5",
        },
    )
    assert argv[argv.index("--effort") + 1] == "max"
    assert argv[argv.index("--fallback-model") + 1] == "claude-fable-5"


# --- per-role effort override -------------------------------------------


def test_per_role_effort_overrides_base(monkeypatch):
    # tester at high, base (plan) at max: the tester provider must use high.
    argv = _capture_claude_argv(
        monkeypatch,
        {"GENIUS_CLAUDE_EFFORT": "max", "GENIUS_CLAUDE_EFFORT_TESTER": "high"},
        role="tester",
    )
    assert argv[argv.index("--effort") + 1] == "high"


def test_per_role_falls_back_to_base(monkeypatch):
    # No _TESTER override -> tester provider uses the base effort.
    argv = _capture_claude_argv(
        monkeypatch, {"GENIUS_CLAUDE_EFFORT": "max"}, role="tester"
    )
    assert argv[argv.index("--effort") + 1] == "max"


def test_architect_role_uses_base_not_tester_override(monkeypatch):
    # The plan (claude/architect) must NOT pick up the tester override.
    argv = _capture_claude_argv(
        monkeypatch,
        {"GENIUS_CLAUDE_EFFORT": "max", "GENIUS_CLAUDE_EFFORT_TESTER": "high"},
        role="claude",
    )
    assert argv[argv.index("--effort") + 1] == "max"
