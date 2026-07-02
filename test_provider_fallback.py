"""Tests for provider fallback chains (ag_core/provider_factory.py):
chain resolution env knobs, the lazy sticky FallbackProvider, the rewired
construction sites (MCP path), and the doctor's chain report."""

import asyncio
import logging
import os
import sys

import pytest

from ag_core import diagnostics, provider_factory
from ag_core.config import load_config
from ag_core.provider_factory import (
    DEFAULT_CHAINS,
    FallbackProvider,
    LEGACY_BACKENDS,
    make_provider,
    resolve_chain,
)
from ag_core.providers.anthropic_provider import AnthropicProvider
from ag_core.providers.grok_provider import GrokProvider
from ag_core.providers.openai_provider import OpenAIProvider
from ag_core.utils.cli_runner import CLITimeoutError

_ENV_VARS = [f"GENIUS_PROVIDER_{r.upper()}" for r in DEFAULT_CHAINS] + [
    "GENIUS_PROVIDER_FALLBACK"
]


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    """Start every test from the no-knobs-set default."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# --- chain resolution ------------------------------------------------------


def test_legacy_defaults_single_backend_per_role():
    assert resolve_chain("grok") == ["grok"]
    assert resolve_chain("claude") == ["claude"]
    for role in ("codex", "tester", "security", "devops"):
        assert resolve_chain(role) == ["codex"]


def test_blank_env_values_treated_as_unset(monkeypatch):
    # Blank vars shipped in .env.example land in os.environ as "" - they must
    # not enable fallback or be parsed as an (empty) explicit chain.
    monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", "")
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "   ")
    assert resolve_chain("grok") == ["grok"]


def test_fallback_env_enables_default_chains(monkeypatch):
    for truthy in ("1", "true", "auto"):
        monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", truthy)
        assert resolve_chain("grok") == ["grok", "claude", "codex"]
        assert resolve_chain("claude") == ["claude", "codex"]
        assert resolve_chain("tester") == ["codex", "claude"]
    monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", "0")
    assert resolve_chain("grok") == ["grok"]


def test_explicit_role_env_wins_over_fallback_env(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", "1")
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "claude,codex")
    assert resolve_chain("grok") == ["claude", "codex"]
    # Other roles still follow the fallback default.
    assert resolve_chain("claude") == ["claude", "codex"]


def test_explicit_role_env_normalizes_names(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_DEVOPS", " Claude , CODEX ")
    assert resolve_chain("devops") == ["claude", "codex"]


def test_unknown_backend_name_raises_actionable_error(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "claude,gpt5")
    with pytest.raises(ValueError) as exc_info:
        resolve_chain("grok")
    msg = str(exc_info.value)
    assert "GENIUS_PROVIDER_GROK" in msg
    assert "gpt5" in msg
    assert "claude" in msg and "codex" in msg and "grok" in msg  # valid names


def test_unknown_role_raises():
    with pytest.raises(ValueError, match="Unknown role"):
        resolve_chain("bard")


def test_chain_source_reports_active_knob(monkeypatch):
    assert provider_factory.chain_source("grok") is None
    monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", "1")
    assert provider_factory.chain_source("grok") == "GENIUS_PROVIDER_FALLBACK=1"
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "claude")
    assert provider_factory.chain_source("grok") == "GENIUS_PROVIDER_GROK"


# --- make_provider ---------------------------------------------------------


def test_legacy_path_returns_raw_provider_classes():
    config = load_config()
    expected = {"grok": GrokProvider, "claude": AnthropicProvider}
    for role in LEGACY_BACKENDS:
        provider = make_provider(role, config)
        assert not isinstance(provider, FallbackProvider)
        assert isinstance(provider, expected.get(role, OpenAIProvider))


def test_fallback_path_returns_fallback_provider(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", "1")
    provider = make_provider("grok", load_config())
    assert isinstance(provider, FallbackProvider)
    assert provider.backend_names == ["grok", "claude", "codex"]


def test_legacy_backend_override_for_mcp_deploy(monkeypatch):
    # The MCP deploy tool historically used the claude backend for the devops
    # role; the override applies only when no env knob is set.
    config = load_config()
    provider = make_provider("devops", config, legacy_backend="claude")
    assert isinstance(provider, AnthropicProvider)
    monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", "1")
    provider = make_provider("devops", config, legacy_backend="claude")
    assert isinstance(provider, FallbackProvider)
    assert provider.backend_names == ["codex", "claude"]


def test_single_element_explicit_chain_returns_raw_provider(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "claude")
    provider = make_provider("grok", load_config())
    assert isinstance(provider, AnthropicProvider)
    assert not isinstance(provider, FallbackProvider)


# --- FallbackProvider ------------------------------------------------------


class _FakeProvider:
    """send_prompt spy: raises ``error`` if set, else returns ``content``."""

    def __init__(self, name, content=None, error=None):
        self.model_name = f"model-{name}"
        self.content = content
        self.error = error
        self.calls = 0

    async def send_prompt(self, prompt, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"content": self.content, "usage": {}}


def _spy_chain(*specs):
    """Build (FallbackProvider, providers, constructed) from (name, provider)
    pairs, with factory-call spies recording which backends got constructed."""
    constructed = []
    providers = {}

    def factory_for(name, prov):
        def factory():
            constructed.append(name)
            return prov

        return factory

    backends = []
    for name, prov in specs:
        providers[name] = prov
        backends.append((name, factory_for(name, prov)))
    return FallbackProvider("grok", backends), providers, constructed


def test_primary_success_never_constructs_fallback():
    fp, providers, constructed = _spy_chain(
        ("grok", _FakeProvider("grok", content="from-grok")),
        ("claude", _FakeProvider("claude", content="from-claude")),
    )
    res = asyncio.run(fp.send_prompt("hi"))
    assert res["content"] == "from-grok"
    assert constructed == ["grok"]  # claude factory never called
    assert providers["claude"].calls == 0


def test_primary_runtime_error_falls_back_and_warns(caplog):
    fp, providers, _ = _spy_chain(
        ("grok", _FakeProvider("grok", error=RuntimeError("403 out of credits"))),
        ("claude", _FakeProvider("claude", content="from-claude")),
    )
    with caplog.at_level(logging.WARNING, logger="ag_core"):
        res = asyncio.run(fp.send_prompt("hi"))
    assert res["content"] == "from-claude"
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "[provider-fallback]" in warning
    assert "'grok' failed" in warning
    assert "403 out of credits" in warning
    assert "trying 'claude'" in warning


def test_cli_timeout_error_also_falls_back():
    fp, _, _ = _spy_chain(
        ("grok", _FakeProvider("grok", error=CLITimeoutError("grok timed out"))),
        ("claude", _FakeProvider("claude", content="from-claude")),
    )
    res = asyncio.run(fp.send_prompt("hi"))
    assert res["content"] == "from-claude"


def test_all_backends_failing_raises_listing_backends():
    fp, _, _ = _spy_chain(
        ("grok", _FakeProvider("grok", error=RuntimeError("grok boom"))),
        ("claude", _FakeProvider("claude", error=RuntimeError("claude boom"))),
    )
    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(fp.send_prompt("hi"))
    msg = str(exc_info.value)
    assert "all backends failed" in msg
    assert "grok" in msg and "claude" in msg
    assert "claude boom" in msg  # the LAST error is surfaced
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_sticky_success_skips_failed_primary_on_next_call():
    grok = _FakeProvider("grok", error=RuntimeError("dead"))
    claude = _FakeProvider("claude", content="from-claude")
    fp, _, _ = _spy_chain(("grok", grok), ("claude", claude))

    asyncio.run(fp.send_prompt("first"))
    asyncio.run(fp.send_prompt("second"))

    assert grok.calls == 1  # not re-paid on the second prompt
    assert claude.calls == 2


def test_cancelled_error_is_not_swallowed():
    grok = _FakeProvider("grok", error=asyncio.CancelledError())
    claude = _FakeProvider("claude", content="never")
    fp, _, constructed = _spy_chain(("grok", grok), ("claude", claude))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fp.send_prompt("hi"))
    assert claude.calls == 0
    assert constructed == ["grok"]


def test_model_name_delegates_to_active_backend():
    grok = _FakeProvider("grok", error=RuntimeError("dead"))
    claude = _FakeProvider("claude", content="ok")
    fp, _, _ = _spy_chain(("grok", grok), ("claude", claude))

    assert fp.model_name == "model-grok"  # before any call: the primary
    asyncio.run(fp.send_prompt("hi"))
    assert fp.model_name == "model-claude"  # after success: the survivor


# --- integration: real grok-403 shim -> claude backend content -------------

IS_WINDOWS = sys.platform == "win32"

# Grok shim replicating the real out-of-credits failure (error envelope +
# exit 1); claude shim returning a normal result envelope.
GROK_403 = r"""
import sys, json
msg = ("API error (status 403 Forbidden): personal-team-blocked:"
       "spending-limit: You have run out of credits.")
sys.stdout.buffer.write(
    (json.dumps({"type": "error", "message": msg}) + "\n").encode("utf-8"))
sys.exit(1)
"""

CLAUDE_HAPPY = r"""
import sys, json
sys.stdin.buffer.read()
envelope = {"type": "result", "is_error": False,
            "result": "claude-served-this",
            "usage": {"input_tokens": 2, "output_tokens": 3}}
sys.stdout.buffer.write((json.dumps(envelope) + "\n").encode("utf-8"))
"""


@pytest.fixture
def shim_dir(tmp_path, monkeypatch):
    """Temp dir of fake vendor CLIs, prepended to PATH (same pattern as
    tests/test_realrun_simulation.py)."""
    d = tmp_path / "cli_shims"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d) + os.pathsep + os.environ.get("PATH", ""))
    if IS_WINDOWS:
        pathext = os.environ.get("PATHEXT", "")
        if ".CMD" not in pathext.upper():
            monkeypatch.setenv("PATHEXT", pathext + os.pathsep + ".CMD")
    return d


def _install_shim(shim_dir, name, py_body):
    script = shim_dir / f"{name}_shim_impl.py"
    script.write_text(py_body, encoding="utf-8")
    if IS_WINDOWS:
        wrapper = shim_dir / f"{name}.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="ascii"
        )
    else:
        wrapper = shim_dir / name
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="ascii"
        )
        wrapper.chmod(0o755)


@pytest.mark.asyncio
async def test_grok_out_of_credits_falls_back_to_claude_backend(
    shim_dir, monkeypatch, caplog
):
    """The real machine condition: grok CLI resolves but dies with 403 at
    runtime - the research role must be served by the claude backend."""
    _install_shim(shim_dir, "grok", GROK_403)
    _install_shim(shim_dir, "claude", CLAUDE_HAPPY)
    monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", "1")

    provider = make_provider("grok", load_config())
    assert isinstance(provider, FallbackProvider)

    with caplog.at_level(logging.WARNING, logger="ag_core"):
        res = await provider.send_prompt("research this")

    assert res["content"] == "claude-served-this"
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "[provider-fallback]" in warning and "403" in warning


# --- integration: the MCP path honors the env knobs ------------------------


@pytest.mark.asyncio
async def test_mcp_execute_agent_honors_provider_env(monkeypatch):
    """execute_agent('research') with GENIUS_PROVIDER_GROK=claude must run on
    the anthropic backend - proves the MCP/Antigravity wiring."""
    import mcp_server

    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "claude")

    async def fake_send(self, prompt, **kwargs):
        return {
            "content": "mcp-fallback-ok",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    monkeypatch.setattr(AnthropicProvider, "send_prompt", fake_send)

    result = await mcp_server.execute_agent(
        "research", "what stack?", context={"app.py": "print('x')"}
    )
    assert "mcp-fallback-ok" in result


# --- doctor: provider chain report -----------------------------------------


def _r(cli, status):
    return {
        "cli": cli,
        "dependents": ["Agent"],
        "path": f"/{cli}",
        "status": status,
        "detail": "d",
    }


_ALL_OK = [_r("grok", "OK"), _r("claude", "OK"), _r("codex", "OK")]


def test_doctor_reports_legacy_chains_by_default():
    lines, _ = diagnostics.report_lines(_ALL_OK, skill_key_ok=True)
    text = "\n".join(lines)
    assert "Provider chains" in text
    assert any("role grok" in ln and "-> grok" in ln for ln in lines)
    # Legacy default: no knob annotation on any role line.
    assert not any("role " in ln and "(GENIUS_PROVIDER" in ln for ln in lines)


def test_doctor_reports_fallback_chains(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", "1")
    lines, _ = diagnostics.report_lines(_ALL_OK, skill_key_ok=True)
    assert any(
        "role grok" in ln
        and "grok, claude, codex" in ln
        and "(GENIUS_PROVIDER_FALLBACK=1)" in ln
        for ln in lines
    )


def test_doctor_reports_explicit_role_chain(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "claude")
    lines, _ = diagnostics.report_lines(_ALL_OK, skill_key_ok=True)
    assert any(
        "role grok" in ln and "-> claude" in ln and "(GENIUS_PROVIDER_GROK)" in ln
        for ln in lines
    )


def test_doctor_warns_when_primary_missing_but_fallback_available(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", "1")
    results = [_r("grok", "MISSING"), _r("claude", "OK"), _r("codex", "OK")]
    lines, _ = diagnostics.report_lines(results, skill_key_ok=True)
    assert any(
        "[warn]" in ln
        and "grok CLI missing" in ln
        and "role grok" in ln
        and "fall back to claude" in ln
        for ln in lines
    )


def test_doctor_reports_bad_env_instead_of_crashing(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "nonsense")
    lines, _ = diagnostics.report_lines(_ALL_OK, skill_key_ok=True)
    assert any("[ERROR]" in ln and "role grok" in ln for ln in lines)
