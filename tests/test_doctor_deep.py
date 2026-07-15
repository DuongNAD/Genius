"""Deep doctor (`serve.py --doctor --deep`): live model canaries (P2 fix).

The shallow doctor only proves CLI binaries answer --version, so it kept
saying READY while agy 1.1.2's model-id rename made every agy call fail and
silently burn the fallback chain. The deep pass sends one canary prompt per
unique (backend, model) pair across the effective role chains and judges each
role by whether ANY backend in its chain is alive.

All canaries here run against stub providers — no real CLI is ever invoked.
"""

import asyncio

import pytest

from ag_core import diagnostics, provider_factory
from ag_core.config import load_config

_ENV_VARS = (
    "GENIUS_CLI_TIMEOUT",
    "GENIUS_DOCTOR_CANARY_TIMEOUT",
    "GENIUS_MODEL_AGY",
    "GENIUS_MODEL_CLAUDE",
    "GENIUS_MODEL_ROLE_RESEARCHER",
    "GENIUS_PROVIDER_RESEARCHER",
    "GENIUS_PROVIDER_CLAUDE",
    "GENIUS_PROVIDER_CODEX",
    "GENIUS_PROVIDER_TESTER",
    "GENIUS_PROVIDER_SECURITY",
    "GENIUS_PROVIDER_DEVOPS",
    "GENIUS_PROVIDER_GROK",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class _StubProvider:
    def __init__(self, content="pong", error=None, delay=0.0):
        self._content = content
        self._error = error
        self._delay = delay

    async def send_prompt(self, prompt, system=None, *, effort=None, **kwargs):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return {"content": self._content, "usage": {}}


# --- header: the timeout line reports the GENERATIVE default ----------------


def test_header_reports_generative_timeout_default(monkeypatch):
    monkeypatch.delenv("GENIUS_CLI_TIMEOUT", raising=False)
    lines, _ = diagnostics._header_lines()
    text = "\n".join(lines)
    assert "GENIUS_CLI_TIMEOUT=600 (default)" in text
    assert "60+" not in text


# --- pair collection ---------------------------------------------------------


def test_collect_canary_pairs_dedupes_and_tracks_roles(monkeypatch):
    monkeypatch.setenv("GENIUS_MODEL_ROLE_RESEARCHER", "Gemini 3.1 Pro (High)")
    monkeypatch.setenv("GENIUS_MODEL_AGY", "Gemini 3.5 Flash (Medium)")
    monkeypatch.setenv("GENIUS_PROVIDER_RESEARCHER", "agy,claude")
    monkeypatch.setenv("GENIUS_PROVIDER_CODEX", "agy,claude")

    pairs, role_chains = diagnostics.collect_canary_pairs(load_config())

    assert role_chains["researcher"] == ["agy", "claude"]
    # researcher's per-role pin is its own pair; codex shares the per-backend
    # agy model with any other agy user, deduped into one entry.
    researcher_pair = pairs[("agy", "Gemini 3.1 Pro (High)")]
    assert researcher_pair["roles"] == ["researcher"]
    assert researcher_pair["primary_for"] == ["researcher"]
    flash_pair = pairs[("agy", "Gemini 3.5 Flash (Medium)")]
    assert "codex" in flash_pair["roles"]
    # Every role appears somewhere.
    seen_roles = {r for entry in pairs.values() for r in entry["roles"]}
    assert seen_roles == set(provider_factory.DEFAULT_CHAINS)


# --- single canary ------------------------------------------------------------


@pytest.mark.asyncio
async def test_canary_call_ok(monkeypatch):
    monkeypatch.setattr(
        provider_factory, "build_backend", lambda b, c, role=None: _StubProvider()
    )
    result = await diagnostics._canary_call("agy", "m", "researcher", object())
    assert result["status"] == "OK"
    assert result["detail"] == "pong"
    assert result["elapsed"] >= 0.0


@pytest.mark.asyncio
async def test_canary_call_failure_captures_error(monkeypatch):
    monkeypatch.setattr(
        provider_factory,
        "build_backend",
        lambda b, c, role=None: _StubProvider(
            error=RuntimeError('invalid --model "gemini-3.1-pro"')
        ),
    )
    result = await diagnostics._canary_call("agy", "gemini-3.1-pro", "researcher", object())
    assert result["status"] == "FAIL"
    assert "invalid --model" in result["detail"]


@pytest.mark.asyncio
async def test_canary_call_empty_response_fails(monkeypatch):
    monkeypatch.setattr(
        provider_factory,
        "build_backend",
        lambda b, c, role=None: _StubProvider(content="   "),
    )
    result = await diagnostics._canary_call("agy", "m", "researcher", object())
    assert result["status"] == "FAIL"
    assert result["detail"] == "empty response"


@pytest.mark.asyncio
async def test_canary_call_timeout(monkeypatch):
    monkeypatch.setenv("GENIUS_DOCTOR_CANARY_TIMEOUT", "0.05")
    monkeypatch.setattr(
        provider_factory,
        "build_backend",
        lambda b, c, role=None: _StubProvider(delay=0.5),
    )
    result = await diagnostics._canary_call("agy", "m", "researcher", object())
    assert result["status"] == "FAIL"
    assert "GENIUS_DOCTOR_CANARY_TIMEOUT" in result["detail"]


# --- report + verdicts ---------------------------------------------------------


def _pair(backend, model, status, roles, primary_for=()):
    return {
        "backend": backend,
        "model": model,
        "status": status,
        "detail": "d",
        "elapsed": 0.1,
        "roles": list(roles),
        "primary_for": list(primary_for),
    }


def test_deep_report_warns_on_dead_primary_with_live_fallback():
    deep = {
        "pairs": [
            _pair("agy", "bad-model", "FAIL", ["researcher"], ["researcher"]),
            _pair("claude", "opus", "OK", ["researcher"]),
        ],
        "role_chains": {"researcher": ["agy", "claude"]},
    }
    lines, code = diagnostics.deep_report_lines(deep)
    text = "\n".join(lines)
    assert code == 0
    assert "fall back to claude" in text
    assert "every role has at least one live backend" in text


def test_deep_report_dead_role_is_fatal():
    deep = {
        "pairs": [
            _pair("agy", "bad", "FAIL", ["researcher"], ["researcher"]),
            _pair("claude", "worse", "FAIL", ["researcher"]),
        ],
        "role_chains": {"researcher": ["agy", "claude"]},
    }
    lines, code = diagnostics.deep_report_lines(deep)
    text = "\n".join(lines)
    assert code == 1
    assert "NOT READY" in text
    assert "researcher" in text


def test_deep_report_unresolvable_chain_is_fatal():
    deep = {"pairs": [], "role_chains": {"tester": []}}
    lines, code = diagnostics.deep_report_lines(deep)
    assert code == 1
    assert any("chain failed to resolve" in ln for ln in lines)


# --- end to end (stubbed) -------------------------------------------------------


@pytest.mark.asyncio
async def test_run_deep_doctor_all_alive(monkeypatch):
    monkeypatch.setattr(
        provider_factory, "build_backend", lambda b, c, role=None: _StubProvider()
    )
    deep = await diagnostics.run_deep_doctor_async()
    assert deep["pairs"], "expected at least one canary pair"
    assert all(r["status"] == "OK" for r in deep["pairs"])
    lines, code = diagnostics.deep_report_lines(deep)
    assert code == 0


@pytest.mark.asyncio
async def test_report_async_deep_flag_appends_section(monkeypatch, capsys):
    monkeypatch.setattr(
        provider_factory, "build_backend", lambda b, c, role=None: _StubProvider()
    )
    code = await diagnostics.run_doctor_report_async(deep=True)
    out = capsys.readouterr().out
    assert "Deep doctor - live model canaries" in out
    assert isinstance(code, int)
