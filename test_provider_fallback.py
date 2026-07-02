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
    make_provider,
    resolve_chain,
)
from ag_core.providers.anthropic_provider import AnthropicProvider
from ag_core.providers.grok_provider import GrokProvider
from ag_core.providers.openai_provider import OpenAIProvider
from ag_core.utils.cli_runner import CLITimeoutError

_ENV_VARS = [f"GENIUS_PROVIDER_{r.upper()}" for r in DEFAULT_CHAINS] + [
    "GENIUS_PROVIDER_FALLBACK",
    # Legacy spelling for the researcher role (old role id "grok").
    "GENIUS_PROVIDER_GROK",
]


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    """Start every test from the no-knobs-set default."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# --- chain resolution ------------------------------------------------------

# The no-env default chain for every role: the grok backend is in none of
# them (opt-in only), agy (Antigravity/Gemini) is the researcher primary.
EXPECTED_DEFAULT_CHAINS = {
    "researcher": ["agy", "claude", "codex"],
    "claude": ["claude", "agy", "codex"],
    "codex": ["codex", "claude", "agy"],
    "tester": ["codex", "claude", "agy"],
    "security": ["codex", "claude", "agy"],
    "devops": ["codex", "claude", "agy"],
}


def test_default_chains_no_env_for_all_six_roles():
    for role, chain in EXPECTED_DEFAULT_CHAINS.items():
        assert resolve_chain(role) == chain
    assert DEFAULT_CHAINS == EXPECTED_DEFAULT_CHAINS


def test_legacy_grok_role_id_is_an_alias_for_researcher(monkeypatch):
    # The Researcher role was renamed grok -> researcher; the old id keeps
    # resolving everywhere through canonical_role.
    assert provider_factory.canonical_role("grok") == "researcher"
    assert provider_factory.canonical_role("grok_researcher") == "researcher"
    assert resolve_chain("grok") == resolve_chain("researcher")

    # GENIUS_PROVIDER_RESEARCHER wins over the legacy GENIUS_PROVIDER_GROK.
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "claude")
    monkeypatch.setenv("GENIUS_PROVIDER_RESEARCHER", "agy,codex")
    assert resolve_chain("researcher") == ["agy", "codex"]
    assert provider_factory.chain_source("researcher") == "GENIUS_PROVIDER_RESEARCHER"
    # Legacy env alone is still honored (queried via either role id).
    monkeypatch.delenv("GENIUS_PROVIDER_RESEARCHER")
    assert resolve_chain("grok") == ["claude"]
    assert provider_factory.chain_source("grok") == "GENIUS_PROVIDER_GROK"


def test_grok_backend_absent_from_every_default_chain():
    for chain in DEFAULT_CHAINS.values():
        assert "grok" not in chain


def test_blank_env_values_treated_as_unset(monkeypatch):
    # Blank vars shipped in .env.example land in os.environ as "" - they must
    # not be parsed as an (empty) explicit chain.
    monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", "")
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "   ")
    assert resolve_chain("grok") == ["agy", "claude", "codex"]


def test_fallback_env_is_deprecated_noop(monkeypatch):
    # GENIUS_PROVIDER_FALLBACK is accepted for backward compat but ignored:
    # truthy, falsy and unset all yield the same default chains.
    for value in ("1", "true", "auto", "0", "false", "off"):
        monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", value)
        for role, chain in EXPECTED_DEFAULT_CHAINS.items():
            assert resolve_chain(role) == chain


def test_explicit_role_env_wins_over_default_chain(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", "1")
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "claude,codex")
    assert resolve_chain("grok") == ["claude", "codex"]
    # Other roles still follow the default chain.
    assert resolve_chain("claude") == ["claude", "agy", "codex"]


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
    # The deprecated fallback knob is never reported (it is a no-op).
    monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", "1")
    assert provider_factory.chain_source("grok") is None
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "claude")
    assert provider_factory.chain_source("grok") == "GENIUS_PROVIDER_GROK"


# --- make_provider ---------------------------------------------------------


def test_default_path_returns_fallback_provider_for_all_roles():
    config = load_config()
    for role, chain in EXPECTED_DEFAULT_CHAINS.items():
        provider = make_provider(role, config)
        assert isinstance(provider, FallbackProvider)
        assert provider.backend_names == chain


def test_explicit_grok_backend_still_builds_grok_provider(monkeypatch):
    # The grok backend stays registered and functional for explicit opt-in.
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "grok")
    provider = make_provider("grok", load_config())
    assert isinstance(provider, GrokProvider)
    assert not isinstance(provider, FallbackProvider)


def test_explicit_grok_first_chain_opt_in(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "grok,agy")
    provider = make_provider("grok", load_config())
    assert isinstance(provider, FallbackProvider)
    assert provider.backend_names == ["grok", "agy"]


def test_default_chain_override_for_mcp_deploy(monkeypatch):
    # The MCP deploy tool keeps its claude-first tradition through an
    # explicit default-chain override.
    config = load_config()
    provider = make_provider("devops", config, default_chain=["claude", "codex", "agy"])
    assert isinstance(provider, FallbackProvider)
    assert provider.backend_names == ["claude", "codex", "agy"]
    # The deprecated fallback knob changes nothing.
    monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", "1")
    provider = make_provider("devops", config, default_chain=["claude", "codex", "agy"])
    assert provider.backend_names == ["claude", "codex", "agy"]
    # An explicit per-role env chain still wins over the call-site override.
    monkeypatch.setenv("GENIUS_PROVIDER_DEVOPS", "codex")
    provider = make_provider("devops", config, default_chain=["claude", "codex", "agy"])
    assert isinstance(provider, OpenAIProvider)


def test_mcp_deploy_tool_uses_claude_first_chain():
    import mcp_server

    role, _agent_cls, default_chain = mcp_server.TOOL_AGENTS["deploy"]
    assert role == "devops"
    assert default_chain == ["claude", "codex", "agy"]
    provider = make_provider(role, load_config(), default_chain=default_chain)
    assert isinstance(provider, FallbackProvider)
    assert provider.backend_names == ["claude", "codex", "agy"]


def test_single_element_explicit_chain_returns_raw_provider(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "claude")
    provider = make_provider("grok", load_config())
    assert isinstance(provider, AnthropicProvider)
    assert not isinstance(provider, FallbackProvider)


def test_genius_model_env_overrides_config_model(monkeypatch):
    # GENIUS_MODEL_<BACKEND> beats config.models.<backend>; blank = unset.
    from ag_core.provider_factory import build_backend

    monkeypatch.setenv("GENIUS_MODEL_CLAUDE", "claude-fable-5")
    provider = build_backend("claude", load_config())
    assert provider.model_name == "claude-fable-5"

    monkeypatch.setenv("GENIUS_MODEL_CLAUDE", "")
    provider = build_backend("claude", load_config())
    assert provider.model_name == load_config().models.anthropic


def test_explicit_agy_backend_builds_keyless_provider(monkeypatch):
    # The agy backend has no config api-key attr; build_backend must tolerate
    # a keyless backend instead of crashing on key_attr.upper().
    from ag_core.providers.agy_provider import AgyProvider

    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "agy")
    provider = make_provider("grok", load_config())
    assert isinstance(provider, AgyProvider)
    assert not isinstance(provider, FallbackProvider)


@pytest.mark.asyncio
async def test_explicit_grok_agy_chain_falls_back_to_agy(monkeypatch):
    """Chain [grok, agy]: grok dying at runtime is served by the agy backend."""
    from ag_core.providers.agy_provider import AgyProvider

    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "grok,agy")

    async def grok_dies(self, prompt, **kwargs):
        raise RuntimeError("403 out of credits")

    async def agy_answers(self, prompt, **kwargs):
        return {"content": "agy-served-this", "usage": {}}

    monkeypatch.setattr(GrokProvider, "send_prompt", grok_dies)
    monkeypatch.setattr(AgyProvider, "send_prompt", agy_answers)

    provider = make_provider("grok", load_config())
    assert isinstance(provider, FallbackProvider)
    assert provider.backend_names == ["grok", "agy"]

    res = await provider.send_prompt("research this")
    assert res["content"] == "agy-served-this"


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

# agy shim that is installed but dead (plain-text CLI, exit 1) - keeps the
# chain hermetic: without it the real machine's agy.exe would serve the
# prompt before claude ever got a chance.
AGY_DEAD = r"""
import sys
sys.stdin.buffer.read()
sys.stderr.buffer.write(b"agy: request failed: 401 unauthorized\n")
sys.exit(1)
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
    """The historical machine condition: an opt-in grok-first chain whose grok
    CLI dies with 403 at runtime (and agy is dead too) - the research role
    must be served by the claude backend, two fall-throughs down the chain.
    The chain is pinned explicitly (the default chain no longer contains
    grok)."""
    _install_shim(shim_dir, "grok", GROK_403)
    _install_shim(shim_dir, "agy", AGY_DEAD)
    _install_shim(shim_dir, "claude", CLAUDE_HAPPY)
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "grok,agy,claude")

    provider = make_provider("grok", load_config())
    assert isinstance(provider, FallbackProvider)
    assert provider.backend_names == ["grok", "agy", "claude"]

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


_ALL_OK = [
    _r("grok", "OK"),
    _r("claude", "OK"),
    _r("codex", "OK"),
    _r("agy", "OK"),
]


def test_doctor_reports_default_chains_by_default():
    lines, _ = diagnostics.report_lines(_ALL_OK, skill_key_ok=True)
    text = "\n".join(lines)
    assert "Provider chains" in text
    assert any("role researcher" in ln and "agy, claude, codex" in ln for ln in lines)
    # Default chains: no knob annotation on any role line.
    assert not any("role " in ln and "(GENIUS_PROVIDER" in ln for ln in lines)


def test_doctor_reports_same_default_chains_with_deprecated_knob(monkeypatch):
    # The deprecated GENIUS_PROVIDER_FALLBACK knob changes nothing and is not
    # reported as a chain source.
    monkeypatch.setenv("GENIUS_PROVIDER_FALLBACK", "1")
    lines, _ = diagnostics.report_lines(_ALL_OK, skill_key_ok=True)
    assert any("role researcher" in ln and "agy, claude, codex" in ln for ln in lines)
    assert not any("GENIUS_PROVIDER_FALLBACK" in ln and "role " in ln for ln in lines)


def test_doctor_reports_explicit_role_chain(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "claude")
    lines, _ = diagnostics.report_lines(_ALL_OK, skill_key_ok=True)
    assert any(
        "role researcher" in ln and "-> claude" in ln and "(GENIUS_PROVIDER_GROK)" in ln
        for ln in lines
    )


def test_doctor_warns_when_primary_missing_but_fallback_available():
    # agy (the default researcher primary) is missing: the warn must name the
    # first backend that actually resolved.
    results = [
        _r("grok", "MISSING"),
        _r("agy", "MISSING"),
        _r("claude", "OK"),
        _r("codex", "OK"),
    ]
    lines, _ = diagnostics.report_lines(results, skill_key_ok=True)
    assert any(
        "[warn]" in ln
        and "agy CLI missing" in ln
        and "role researcher" in ln
        and "fall back to claude" in ln
        for ln in lines
    )


def test_doctor_reports_bad_env_instead_of_crashing(monkeypatch):
    monkeypatch.setenv("GENIUS_PROVIDER_GROK", "nonsense")
    lines, _ = diagnostics.report_lines(_ALL_OK, skill_key_ok=True)
    assert any("[ERROR]" in ln and "role researcher" in ln for ln in lines)
